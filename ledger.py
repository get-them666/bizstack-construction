"""Double-entry general ledger.

Everything else in this app reports on money. This is where money actually
gets recorded. It covers both jobs and crew, because they are the same
problem in opposite directions: a customer payment is revenue against a job,
and a crew payment is a wage against that same job. Keeping them in one
ledger is what makes the P&L per job true instead of estimated.

WHY DOUBLE-ENTRY AND NOT MORE SPREADSHEETS
------------------------------------------
The cost model already answers "what did this job cost". That is a single
entry view: numbers land wherever someone typed them. Double entry forces
every dollar to move from one account to another, so a typo cannot create
money. If debits and credits do not agree, the post is refused. That
constraint is the entire reason to do this properly rather than adding
another report.

FOUR RULES, ALL ENFORCED IN CODE
--------------------------------
1. MONEY IS INTEGER CENTS. Never float, never Decimal. 0.1 + 0.2 problems
   are not acceptable in a ledger that feeds a tax return. Every amount in
   and out is an int.

2. AN ENTRY MUST BALANCE. Sum(debits) == sum(credits), exactly. If it does
   not, the post raises and nothing is written. This is checked before any
   INSERT, inside the transaction.

3. A LINE IS EITHER DEBIT OR CREDIT, never both, never neither. A line
   with 0/0 is a bug that would silently distort a balance.

4. POSTED IS PERMANENT. Entries are append-only. Corrections are reversing
   entries, not edits -- which is how an auditor expects to see it and how
   you reconcile a mistake later.

IDEMPOTENCY
-----------
Postings originate from other tables (an invoice, a cost line, a payroll
run). Re-posting the same source must not double-count, so (company,
source_type, source_id) is unique. `post()` returns whether it actually
wrote anything, which is what makes it safe to call on every save.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# --- account types ----------------------------------------------------------
ASSET = "asset"
LIABILITY = "liability"
EQUITY = "equity"
REVENUE = "revenue"
EXPENSE = "expense"

# Normal balance: which side increases the account.
_DEBIT_NORMAL = {ASSET, EXPENSE}
_CREDIT_NORMAL = {LIABILITY, EQUITY, REVENUE}

TYPES = [ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE]

# --- the chart --------------------------------------------------------------
# Construction-shaped. Direct job costs are kept separate from overhead so a
# job margin is real: revenue less its own material, labor, subs and
# equipment, not revenue less a tenth of everything.
CHART: list[tuple] = [
    # code, name, type
    ("1010", "Cash — Business Checking", ASSET),
    ("1020", "Cash — Card Clearing", ASSET),
    ("1030", "Cash — Payroll Funding", ASSET),
    ("1100", "Accounts Receivable", ASSET),
    ("1150", "Customer Deposits Receivable", ASSET),
    ("1200", "Materials Inventory", ASSET),
    ("1250", "Work in Progress", ASSET),
    ("1300", "Prepaid Expenses", ASSET),

    ("2010", "Accounts Payable", LIABILITY),
    ("2050", "Credit Card Payable", LIABILITY),
    ("2100", "Customer Deposits Held", LIABILITY),
    ("2150", "Payroll Payable", LIABILITY),
    ("2200", "Sales Tax Payable", LIABILITY),

    ("3010", "Owner's Capital", EQUITY),
    ("3020", "Owner's Draws", EQUITY),
    ("3030", "Retained Earnings", EQUITY),

    ("4010", "Contract Revenue", REVENUE),
    ("4020", "Change Order Revenue", REVENUE),
    ("4090", "Deposit Revenue", REVENUE),

    ("5010", "Materials — Direct", EXPENSE),
    ("5020", "Labor — Direct", EXPENSE),
    ("5030", "Subcontractor — Direct", EXPENSE),
    ("5040", "Equipment — Direct", EXPENSE),
    ("5050", "Permits and Fees — Direct", EXPENSE),

    ("6010", "Office and Administrative", EXPENSE),
    ("6020", "Vehicle and Fuel", EXPENSE),
    ("6030", "Insurance", EXPENSE),
    ("6040", "Rent", EXPENSE),
    ("6050", "Marketing", EXPENSE),
    ("6060", "Bank and Merchant Fees", EXPENSE),
    ("6070", "Depreciation", EXPENSE),
]

# Cost category -> direct-cost account, mirroring bid_costing.COST_CATEGORIES.
CATEGORY_ACCOUNT = {
    "material": "5010",
    "labor": "5020",
    "sub": "5030",
    "equipment": "5040",
    "permit": "5050",
    "other": "6010",
}


class LedgerError(RuntimeError):
    """Anything that would make the books wrong. Never swallowed."""


def table_exists(conn, name: str) -> bool:
    """Whether a table is present.

    Needed because payroll_runs / payroll_lines / crew are created by
    construction_main's startup DDL, not by this module. On a database where
    the app has not booted, a bare SELECT against one of them aborts the
    caller's transaction -- so the payroll sync checks first rather than
    failing inside a try/except that has already poisoned the connection.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) AS t", (name,))
        row = cur.fetchone()
    return bool(row and row["t"])


# Internal alias kept so bid_costing and ledger_sync can share one helper.
_table_exists = table_exists


def _now():
    return datetime.now(timezone.utc)


def _c(v, field="amount") -> int:
    """Coerce to integer cents. Refuses anything that is not exactly a cent."""
    if v is None:
        raise LedgerError(f"{field} is required")
    if isinstance(v, bool):
        raise LedgerError(f"{field} must be a number, not a bool")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # float money is the exact thing this module exists to prevent.
        # Reject a fractional cent outright rather than rounding silently.
        if abs(v - round(v, 2)) > 1e-9:
            raise LedgerError(f"{field} has sub-cent precision: {v}")
        return int(round(v * 100))
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        try:
            f = float(s)
        except ValueError:
            raise LedgerError(f"{field} is not a number: {v!r}") from None
        return _c(f, field)
    if hasattr(v, "quantize"):          # Decimal
        return int(round(v * 100)) if hasattr(v, "as_tuple") else _c(float(v), field)
    raise LedgerError(f"{field} is not a number: {v!r}")


# --- schema -----------------------------------------------------------------
def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_accounts (
                id SERIAL PRIMARY KEY,
                code VARCHAR(10) NOT NULL,
                name VARCHAR(160) NOT NULL,
                type VARCHAR(20) NOT NULL,
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_accounts_code
                ON ledger_accounts(company, code)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id SERIAL PRIMARY KEY,
                entry_no VARCHAR(40) NOT NULL,
                entry_date DATE NOT NULL,
                memo TEXT,
                source_type VARCHAR(40),
                source_id VARCHAR(80),
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # CREATE TABLE IF NOT EXISTS will happily skip a table that already
        # exists from an earlier shape, and the indexes below then fail on
        # columns that were never added. That is not hypothetical -- it is
        # what production hit, where a partial ledger_entries survived from a
        # deploy that failed partway. Enumerating columns one at a time as
        # each new error appears is how you ship three fixes for one problem,
        # so every column the CREATE TABLE declares and the indexes rely on
        # is asserted here in one go.
        for _col, _decl in (
            ("entry_no", "VARCHAR(40)"),
            ("entry_date", "DATE"),
            ("memo", "TEXT"),
            ("source_type", "VARCHAR(40)"),
            ("source_id", "VARCHAR(80)"),
            ("company", "VARCHAR(40) DEFAULT 'construction'"),
            ("created_at", "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"),
        ):
            cur.execute(f"ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS {_col} {_decl};")

        # tx_type is asserted and defaulted, not just declared. A live database
        # predating the double-entry rewrite still carries it as NOT NULL with
        # no default, and CREATE TABLE IF NOT EXISTS will not remove it, so
        # every write to the ledger failed there -- opening the books was just
        # the first caller to notice. The default keeps any other writer alive;
        # post() derives a better value from the entry's own accounts.
        cur.execute("ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS tx_type VARCHAR(20);")
        cur.execute("ALTER TABLE ledger_entries ALTER COLUMN tx_type SET DEFAULT 'journal';")
        cur.execute("UPDATE ledger_entries SET tx_type = 'journal' WHERE tx_type IS NULL;")
        cur.execute("ALTER TABLE ledger_entries ALTER COLUMN tx_type SET NOT NULL;")

        # Idempotency. A repost of the same source record is a no-op rather
        # than a duplicate, which is what makes it safe to call on every save.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_entries_source
                ON ledger_entries(company, source_type, source_id)
                WHERE source_type IS NOT NULL AND source_id IS NOT NULL
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_entries_no
                ON ledger_entries(company, entry_no)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_entries_date
                ON ledger_entries(company, entry_date)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_lines (
                id SERIAL PRIMARY KEY,
                entry_id INTEGER NOT NULL REFERENCES ledger_entries(id) ON DELETE CASCADE,
                account_code VARCHAR(10) NOT NULL,
                debit_cents BIGINT NOT NULL DEFAULT 0,
                credit_cents BIGINT NOT NULL DEFAULT 0,
                description TEXT,
                bid_id INTEGER,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Same reason as the ALTERs above, and same ordering trap: these have
        # to come after the CREATE TABLE, not before it.
        for _col, _decl in (
            ("entry_id", "INTEGER"),
            ("account_code", "VARCHAR(10)"),
            ("debit_cents", "BIGINT NOT NULL DEFAULT 0"),
            ("credit_cents", "BIGINT NOT NULL DEFAULT 0"),
            ("description", "TEXT"),
            ("bid_id", "INTEGER"),
            ("created_at", "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"),
        ):
            cur.execute(f"ALTER TABLE ledger_lines ADD COLUMN IF NOT EXISTS {_col} {_decl};")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_lines_entry ON ledger_lines(entry_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_lines_bid ON ledger_lines(bid_id);")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_lines_account
                ON ledger_lines(account_code, debit_cents, credit_cents)
        """)
    conn.commit()
    seed_chart(conn)


def seed_chart(conn, company: str = "construction") -> int:
    """Idempotent. Adds any account the chart has gained since last boot."""
    added = 0
    with conn.cursor() as cur:
        for code, name, typ in CHART:
            if typ not in TYPES:
                raise LedgerError(f"account {code} has unknown type {typ}")
            cur.execute("""
                INSERT INTO ledger_accounts (code, name, type, company)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (company, code) DO UPDATE SET name = EXCLUDED.name
            """, (code, name, typ, company))
            added += cur.rowcount
    conn.commit()
    return added


def account_id(conn, code: str, company: str = "construction") -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM ledger_accounts WHERE company = %s AND code = %s",
                    (company, code))
        row = cur.fetchone()
    if not row:
        raise LedgerError(f"account {code} is not in the chart")
    return row["id"]


def _next_entry_no(conn, company: str = "construction") -> str:
    year = date.today().year
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger_entries WHERE company = %s", (company,))
        n = cur.fetchone()["n"] + 1
    return f"JE-{year}-{n:06d}"


# --- posting ----------------------------------------------------------------
def post(conn, lines: list[dict], entry_date=None, memo: str = "",
         source_type: str = "", source_id: str = "",
         company: str = "construction", reverse_of: int | None = None) -> dict:
    """Write one balanced entry. Returns {posted, entry_id, entry_no, debits}.

    `lines`: [{"account": "1010", "debit": 50000, "credit": 0,
               "description": "...", "bid_id": 12}]

    Every rule this module claims is checked here before a single row is
    written, so a rejected post leaves nothing behind.
    """
    if not lines:
        raise LedgerError("an entry needs at least one line")

    prepared = []
    for i, ln in enumerate(lines):
        code = str(ln.get("account") or "").strip()
        if not code:
            raise LedgerError(f"line {i}: account is required")
        d = _c(ln.get("debit", 0) or 0, f"line {i} debit")
        c = _c(ln.get("credit", 0) or 0, f"line {i} credit")
        if d < 0 or c < 0:
            raise LedgerError(f"line {i}: negative amounts must be expressed as the opposite side")
        if d and c:
            raise LedgerError(f"line {i} ({code}): a line cannot be both a debit and a credit")
        if not d and not c:
            raise LedgerError(f"line {i} ({code}): a line cannot be zero on both sides")
        prepared.append({"account": code, "debit": d, "credit": c,
                         "description": (ln.get("description") or "")[:500] or None,
                         "bid_id": ln.get("bid_id")})

    debits = sum(x["debit"] for x in prepared)
    credits = sum(x["credit"] for x in prepared)
    if debits != credits:
        raise LedgerError(
            f"entry does not balance: debits {debits/100:,.2f} vs credits {credits/100:,.2f} "
            f"(difference {(debits - credits)/100:,.2f})")

    # Every account referenced must exist, before any write.
    # Seed on demand: the chart is per-company, so a company that has never
    # posted before would otherwise fail with "account 1100 is not in the
    # chart" -- which reads as a broken module rather than a new book.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger_accounts WHERE company = %s", (company,))
        if not cur.fetchone()["n"]:
            seed_chart(conn, company)
    for code in {x["account"] for x in prepared}:
        account_id(conn, code, company)

    when = entry_date or date.today()
    if isinstance(when, str):
        when = date.fromisoformat(when)

    with conn.cursor() as cur:
        if source_type and source_id:
            cur.execute(
                "SELECT id FROM ledger_entries WHERE company = %s AND source_type = %s AND source_id = %s",
                (company, source_type, str(source_id)))
            if cur.fetchone():
                return {"posted": False, "entry_id": None, "entry_no": None,
                        "debits": debits, "reason": "already posted"}
        entry_no = _next_entry_no(conn, company)
        cur.execute(
            "INSERT INTO ledger_entries (entry_no, entry_date, memo, source_type, source_id, "
            "company, tx_type) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (entry_no, when, (memo or "")[:1000] or None,
             source_type or None, str(source_id) if source_id else None, company,
             _tx_type(prepared)))
        eid = cur.fetchone()["id"]
        for x in prepared:
            cur.execute(
                "INSERT INTO ledger_lines (entry_id, account_code, debit_cents, credit_cents, "
                "description, bid_id) VALUES (%s,%s,%s,%s,%s,%s)",
                (eid, x["account"], x["debit"], x["credit"], x["description"], x["bid_id"]))
    conn.commit()
    return {"posted": True, "entry_id": eid, "entry_no": entry_no, "debits": debits}


def _tx_type(prepared: list) -> str:
    """Derive the entry's coarse type from the accounts it touches.

    ``ledger_entries.tx_type`` is a leftover from the pre-double-entry model.
    The column survives in live databases -- ``CREATE TABLE IF NOT EXISTS``
    never drops one -- and it is NOT NULL with no default, so a database that
    still has it rejected every write to the ledger. Opening the books was the
    first thing to trip it, and it was not the only thing.

    It is derived rather than asked for, so an entry cannot be labelled by a
    caller and disagree with its own lines. ``pitch_numbers`` reads this column
    with ``tx_type IN ('income','revenue')`` and ``tx_type = 'expense'``, so
    those are the values used: the entry is an expense when it charges an
    expense account, income when it credits a revenue account, and a plain
    journal otherwise (a transfer, an opening balance, a deposit held).
    """
    expense_accounts, revenue_accounts = set(), set()
    for code, _name, kind in CHART:
        if kind == EXPENSE:
            expense_accounts.add(code)
        elif kind == REVENUE:
            revenue_accounts.add(code)
    debits = {x["account"] for x in prepared if x.get("debit")}
    credits = {x["account"] for x in prepared if x.get("credit")}
    if debits & expense_accounts:
        return "expense"
    if credits & revenue_accounts:
        return "income"
    return "journal"


def post_balanced(conn, debits: dict, credits: dict, **kw) -> dict:
    """Convenience: build the lines from two account->amount maps."""
    lines = [{"account": a, "debit": v, "description": kw.get("description")}
             for a, v in (debits or {}).items()]
    lines += [{"account": a, "credit": v, "description": kw.get("description")}
              for a, v in (credits or {}).items()]
    return post(conn, lines, **kw)


def reverse_entry(conn, entry_id: int, entry_date=None, memo: str = "",
                  company: str = "construction") -> dict:
    """Correct a mistake by reversing it. Entries are never edited."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ledger_entries WHERE id = %s AND company = %s",
                    (entry_id, company))
        entry = cur.fetchone()
        if not entry:
            raise LedgerError("entry not found")
        cur.execute("SELECT * FROM ledger_lines WHERE entry_id = %s", (entry_id,))
        lines = cur.fetchall()
    if not lines:
        raise LedgerError("entry has no lines")
    flipped = [{"account": l["account_code"], "debit": l["credit_cents"],
                "credit": l["debit_cents"], "description": l["description"],
                "bid_id": l["bid_id"]} for l in lines]
    return post(conn, flipped, entry_date=entry_date,
                memo=(memo or f"Reversal of {entry['entry_no']}"),
                company=company, reverse_of=entry_id)


# --- reports ----------------------------------------------------------------
def trial_balance(conn, as_of=None, company: str = "construction") -> dict:
    """Debits == credits. Always. If this does not sum to zero, something is wrong."""
    params: list = [company]
    clause = ""
    if as_of:
        clause = "AND e.entry_date <= %s"
        params.append(as_of)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT l.account_code, a.name, a.type,
                   COALESCE(SUM(l.debit_cents), 0)::bigint AS dr,
                   COALESCE(SUM(l.credit_cents), 0)::bigint AS cr
            FROM ledger_lines l
            JOIN ledger_entries e ON e.id = l.entry_id
            JOIN ledger_accounts a ON a.code = l.account_code AND a.company = e.company
            WHERE e.company = %s {clause}
            GROUP BY l.account_code, a.name, a.type
            HAVING COALESCE(SUM(l.debit_cents), 0)::bigint <> 0 OR COALESCE(SUM(l.credit_cents), 0)::bigint <> 0
            ORDER BY l.account_code
        """, params)
        rows = cur.fetchall()
    out = []
    td = tc = 0
    for r in rows:
        bal = (r["dr"] - r["cr"]) / 100.0
        out.append({"code": r["account_code"], "name": r["name"], "type": r["type"],
                    "debit": r["dr"] / 100.0, "credit": r["cr"] / 100.0, "balance": bal})
        td += r["dr"]
        tc += r["cr"]
    return {"rows": out, "total_debits": td / 100.0, "total_credits": tc / 100.0,
            "difference": (td - tc) / 100.0, "balanced": td == tc}


def _balances(conn, types: tuple, as_of=None, company: str = "construction") -> dict:
    params: list = [company]
    clause = ""
    if as_of:
        clause = "AND e.entry_date <= %s"
        params.append(as_of)
    # Driven from the chart, LEFT JOINed to activity. A balance sheet that
    # silently omits Cash just because it happens to be zero is worse than
    # useless -- it reads as though there is no cash account at all.
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT a.code AS account_code, a.name, a.type,
                   COALESCE(SUM(l.debit_cents), 0)::bigint AS dr,
                   COALESCE(SUM(l.credit_cents), 0)::bigint AS cr
            FROM ledger_accounts a
            LEFT JOIN ledger_lines l ON l.account_code = a.code
            LEFT JOIN ledger_entries e ON e.id = l.entry_id
            WHERE a.company = %s AND a.type = ANY(%s) {clause}
            GROUP BY a.code, a.name, a.type
        """, [company, list(types)] + params[1:])
        rows = cur.fetchall()
    out = {}
    for r in rows:
        if r["type"] in _DEBIT_NORMAL:
            out[r["account_code"]] = {"name": r["name"], "balance": (r["dr"] - r["cr"]) / 100.0}
        else:
            out[r["account_code"]] = {"name": r["name"], "balance": (r["cr"] - r["dr"]) / 100.0}
    return out


def income_statement(conn, start=None, end=None, company: str = "construction") -> dict:
    """Revenue less expense for the period. Every line is a cent integer until here."""
    where = ["e.company = %s"]
    params: list = [company]
    if start:
        where.append("e.entry_date >= %s")
        params.append(start)
    if end:
        where.append("e.entry_date <= %s")
        params.append(end)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT l.account_code, a.name, a.type,
                   COALESCE(SUM(l.debit_cents), 0)::bigint AS dr,
                   COALESCE(SUM(l.credit_cents), 0)::bigint AS cr
            FROM ledger_lines l
            JOIN ledger_entries e ON e.id = l.entry_id
            JOIN ledger_accounts a ON a.code = l.account_code AND a.company = e.company
            WHERE {' AND '.join(where)} AND a.type = ANY(%s)
            GROUP BY l.account_code, a.name, a.type
            ORDER BY a.type, l.account_code
        """, params + [[REVENUE, EXPENSE]])
        rows = cur.fetchall()

    revenue = []
    expense = []
    for r in rows:
        amt = ((r["cr"] - r["dr"]) if r["type"] == REVENUE else (r["dr"] - r["cr"])) / 100.0
        item = {"code": r["account_code"], "name": r["name"], "amount": amt}
        (revenue if r["type"] == REVENUE else expense).append(item)
    total_rev = sum(x["amount"] for x in revenue)
    total_exp = sum(x["amount"] for x in expense)
    direct = [x for x in expense if x["code"].startswith("50")]
    overhead = [x for x in expense if x["code"].startswith("60")]
    return {"revenue": revenue, "expense": expense,
            "direct_cost": sum(x["amount"] for x in direct),
            "overhead": sum(x["amount"] for x in overhead),
            "total_revenue": total_rev, "total_expense": total_exp,
            "net_income": round(total_rev - total_exp, 2)}


def balance_sheet(conn, as_of=None, company: str = "construction") -> dict:
    """Assets = Liabilities + Equity, with earnings included in equity.

    Equity here is the posted equity accounts PLUS current-period earnings.
    Without that addition the sheet does not balance for any period in which
    you have earned something but not yet closed the books -- which is to
    say, for essentially every day you would actually look at it. Revenue
    and expense accounts carry the year's result until close_period moves it
    to Retained Earnings; until then that result is still equity, and a
    balance sheet that leaves it out is simply wrong.
    """
    assets = _balances(conn, (ASSET,), as_of, company)
    liabilities = _balances(conn, (LIABILITY,), as_of, company)
    equity = _balances(conn, (EQUITY,), as_of, company)
    ta = sum(a["balance"] for a in assets.values())
    tl = sum(a["balance"] for a in liabilities.values())

    earnings = income_statement(conn, end=as_of, company=company)["net_income"]
    posted = sum(a["balance"] for a in equity.values())
    te = posted + earnings
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "current_earnings": earnings,
            "total_assets": round(ta, 2), "total_liabilities": round(tl, 2),
            "posted_equity": round(posted, 2), "total_equity": round(te, 2),
            "balanced": round(ta, 2) == round(tl + te, 2),
            "difference": round(ta - tl - te, 2)}


def open_books(conn, cash_cents: int, equity_cents: int = None,
               when=None, company: str = "construction") -> dict:
    """Post the opening balance entry so the books start balanced.

    A ledger with no opening entry reports everything the first time money
    moves, which makes the first trial balance look like a catastrophe that
    was not the books' fault. This puts the starting position in explicitly.
    """
    cash = _c(cash_cents, "opening cash")
    eq = cash if equity_cents is None else _c(equity_cents, "opening equity")
    if cash <= 0:
        return {"posted": False, "reason": "nothing to open with"}
    return post(conn, [
        {"account": "1010", "debit": cash, "description": "Opening balance"},
        {"account": "3010", "credit": eq, "description": "Opening capital"},
    ], entry_date=when, memo="Opening balances",
        source_type="open", source_id="1", company=company)


def close_period(conn, to_date, company: str = "construction") -> dict:
    """Move this period's earnings into Retained Earnings.

    Books are append-only, so closing is a real entry: Dr each revenue and
    expense account, Cr Retained Earnings for the net. That zeroes the
    P&L accounts and rolls the result forward permanently.
    """
    ist = income_statement(conn, end=to_date, company=company)
    lines = []
    for a in ist["revenue"]:
        if a["amount"]:
            lines.append({"account": a["code"], "debit": int(round(a["amount"] * 100)),
                          "description": "Close revenue"})
    for a in ist["expense"]:
        if a["amount"]:
            lines.append({"account": a["code"], "credit": int(round(a["amount"] * 100)),
                          "description": "Close expense"})
    net = ist["net_income"]
    if not lines:
        return {"posted": False, "reason": "nothing to close"}
    lines.append({"account": "3030" if net >= 0 else "3010",
                  "credit" if net >= 0 else "debit": int(round(abs(net) * 100)),
                  "description": "Retained earnings"})
    return post(conn, lines, entry_date=to_date,
                memo=f"Close period to {to_date}", source_type="close", company=company)


def job_pnl(conn, company: str = "construction") -> list[dict]:
    """Revenue, direct cost and margin per job. This is the number that matters."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.bid_id,
                   COALESCE(SUM(l.credit_cents) FILTER (WHERE a.type = 'revenue'), 0)::bigint AS rev,
                   COALESCE(SUM(l.debit_cents)  FILTER (WHERE a.type = 'expense' AND a.code LIKE '50%%'), 0)::bigint AS dc,
                   COALESCE(SUM(l.debit_cents)  FILTER (WHERE a.type = 'expense' AND a.code LIKE '60%%'), 0)::bigint AS oh
            FROM ledger_lines l
            JOIN ledger_entries e ON e.id = l.entry_id
            JOIN ledger_accounts a ON a.code = l.account_code AND a.company = e.company
            WHERE e.company = %s AND l.bid_id IS NOT NULL
            GROUP BY l.bid_id ORDER BY rev DESC
        """, (company,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            rev, dc, oh = r["rev"] / 100.0, r["dc"] / 100.0, r["oh"] / 100.0
            out.append({"bid_id": r["bid_id"], "revenue": rev, "direct_cost": dc,
                        "overhead_allocated": oh,
                        "gross_margin": round(rev - dc, 2),
                        "gross_margin_pct": round((rev - dc) / rev * 100, 1) if rev else None,
                        "net": round(rev - dc - oh, 2)})
        return out


def entries(conn, limit: int = 50, company: str = "construction") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT e.id, e.entry_no, e.entry_date, e.memo, e.source_type, e.source_id,
                   COALESCE(SUM(l.debit_cents), 0)::bigint/100.0 AS amount
            FROM ledger_entries e
            LEFT JOIN ledger_lines l ON l.entry_id = e.id
            WHERE e.company = %s
            GROUP BY e.id ORDER BY e.entry_date DESC, e.id DESC LIMIT %s
        """, (company, limit))
        return cur.fetchall()