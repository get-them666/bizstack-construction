"""PNC Bank CSV importer for BuildStack Construction LLC admin portal.

PNC PINACLE exports business checking/credit card activity in two CSV shapes:

  Format 1 (business checking): Date, Description, Withdrawals, Deposits, Balance
  Format 2 (credit card):       Date, Description, Amount

Both share the same three semantic columns (date, description, amount) once
withdrawals/deposits are folded into a signed amount. The importer accepts
either shape, normalises to a flat row, tags it by keyword, deduplicates via
a SHA-256 hash of (date, amount, description), skips internal transfers and
website payouts already recorded in the ``payments`` table, then writes to the
``transactions`` ledger used by ``tax_engine``.

Nothing here moves money. The importer is a bookkeeping convenience — the real
bank statement is still only authoritative inside PNC PINACLE.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS transactions (
    id              BIGSERIAL PRIMARY KEY,
    tx_date         DATE NOT NULL,
    business_unit   VARCHAR(80) NOT NULL DEFAULT 'BuildStack Construction',
    tx_type         VARCHAR(20) NOT NULL DEFAULT 'expense',
    vendor_or_payee VARCHAR(255),
    description     TEXT,
    amount          NUMERIC(12,2) NOT NULL,
    payment_method  VARCHAR(40) DEFAULT 'Unknown',
    w9_on_file      BOOLEAN DEFAULT FALSE,
    tx_hash         VARCHAR(64) NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
        # Guard column additions for databases created before this module.
        cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tx_hash VARCHAR(64);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS transactions_tx_hash_key ON transactions (tx_hash);")
    conn.commit()


# ---------------------------------------------------------------------------
# Keyword auto-tagging
# ---------------------------------------------------------------------------

_UNIT_RULES: list[tuple[list[str], str]] = [
    (["home depot", "lowes", "lowes foods", "lowe's", "sherwin", "sherwin williams",
      "menards", "ace hardware", "True Value"], "BuildStack Construction"),
    (["turnover", "hospitable", "broom", "vrbo", "airbnb", "guest", "rental",
      "cleaning supply", "bleach", "linen"], "Broom Service"),
]


def auto_tag(description: str) -> str:
    """Return the business unit that owns this description.

    Default is BuildStack Construction (the parent LLC). Only the keywords
    that unambiguously belong to Broom Service flip the tag -- when in doubt
    the money is construction money, because construction buys the supplies
    Broom Service uses.
    """
    blob = (description or "").lower()
    for keywords, unit in _UNIT_RULES:
        if any(kw in blob for kw in keywords):
            return unit
    return "BuildStack Construction"


# ---------------------------------------------------------------------------
# Transfer / payout detection
# ---------------------------------------------------------------------------

_INTERNAL_TRANSFER_RE = re.compile(
    r"\b(transfer|zelle to|zelle from|ach to|ach from|internal|xfer)\b",
    re.IGNORECASE,
)


def is_internal_transfer(description: str) -> bool:
    return bool(_INTERNAL_TRANSFER_RE.search(description or ""))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y")


def _parse_date(raw: str) -> date | None:
    text = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("$", "").replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _signed_amount(withdrawals: str | None, deposits: str | None) -> Decimal | None:
    """Format-1 helper: withdrawals are negative, deposits positive."""
    w = _parse_amount(withdrawals or "")
    d = _parse_amount(deposits or "")
    if w and w > 0:
        return -w
    if d and d > 0:
        return d
    # If both columns were blank the row is useless; if a caller passed a
    # signed amount in either column we still want it.
    if w:
        return -abs(w)
    if d:
        return abs(d)
    return None


def compute_hash(tx_date: date, amount: Decimal, description: str) -> str:
    """SHA-256 of (date, amount, description). Two CSV rows that differ in any
    one of those three fields produce different hashes; identical rows produce
    the same hash and the second insert is skipped as a duplicate.
    """
    payload = f"{tx_date.isoformat()}|{str(amount)}|{(description or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _detect_format(headers: list[str]) -> str:
    """Return 'checking' (5-col) or 'credit' (3-col) or 'unknown'."""
    lowered = [h.strip().lower() for h in headers]
    if any("withdrawal" in h for h in lowered) or any("deposit" in h for h in lowered):
        return "checking"
    if any(h in ("amount", "amt") for h in lowered):
        return "credit"
    return "unknown"


def parse_csv(raw_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (rows, errors). Each row dict has keys matching the transactions
    table columns except ``id``/``created_at``. Errors are human-readable
    strings for rows that could not be parsed at all (blank, malformed date).
    """
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    # PNC sometimes prepends a BOM or wraps fields in odd encodings. UTF-8-sig
    # strips the BOM if present and falls through to plain UTF-8 otherwise.
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1", errors="replace")

    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return rows, ["Empty CSV — no header row."]

    fmt = _detect_format(headers)
    if fmt == "unknown":
        return rows, [
            "Unrecognised CSV format. Expected either "
            "'Date,Description,Withdrawals,Deposits,Balance' or "
            "'Date,Description,Amount'."
        ]

    lowered = [h.strip().lower() for h in headers]
    idx_date = _first_index(lowered, ("date", "posting date", "transaction date"))
    idx_desc = _first_index(lowered, ("description", "memo", "detail"))
    if idx_date is None or idx_desc is None:
        return rows, ["CSV is missing a Date or Description column."]

    if fmt == "checking":
        idx_w = _first_index(lowered, ("withdrawal", "withdrawals", "debit", "debits"))
        idx_d = _first_index(lowered, ("deposit", "deposits", "credit", "credits"))
        idx_balance = _first_index(lowered, ("balance",))
        for row_num, cells in enumerate(reader, start=2):
            if not cells or all(not c.strip() for c in cells):
                continue
            if len(cells) <= max(i for i in (idx_date, idx_desc, idx_w or 0, idx_d or 0) if i is not None):
                errors.append(f"Row {row_num}: not enough columns.")
                continue
            tx_date = _parse_date(cells[idx_date])
            if tx_date is None:
                errors.append(f"Row {row_num}: bad date '{cells[idx_date]}'.")
                continue
            desc = (cells[idx_desc] or "").strip()
            amount = _signed_amount(
                cells[idx_w] if idx_w is not None else "",
                cells[idx_d] if idx_d is not None else "",
            )
            if amount is None:
                errors.append(f"Row {row_num}: no withdrawal or deposit amount.")
                continue
            rows.append(_build_row(tx_date, desc, amount))

    else:  # credit-card format
        idx_amt = _first_index(lowered, ("amount", "amt"))
        if idx_amt is None:
            return rows, ["Credit-card CSV is missing the Amount column."]
        for row_num, cells in enumerate(reader, start=2):
            if not cells or all(not c.strip() for c in cells):
                continue
            if len(cells) <= max(idx_date, idx_desc, idx_amt):
                errors.append(f"Row {row_num}: not enough columns.")
                continue
            tx_date = _parse_date(cells[idx_date])
            if tx_date is None:
                errors.append(f"Row {row_num}: bad date '{cells[idx_date]}'.")
                continue
            desc = (cells[idx_desc] or "").strip()
            amount = _parse_amount(cells[idx_amt])
            if amount is None:
                errors.append(f"Row {row_num}: bad amount '{cells[idx_amt]}'.")
                continue
            rows.append(_build_row(tx_date, desc, amount))

    return rows, errors


def _first_index(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for cand in candidates:
        for i, h in enumerate(headers):
            if cand in h:
                return i
    return None


def _build_row(tx_date: date, description: str, amount: Decimal) -> dict[str, Any]:
    business_unit = auto_tag(description)
    tx_type = "income" if amount > 0 else "expense"
    return {
        "tx_date": tx_date,
        "business_unit": business_unit,
        "tx_type": tx_type,
        "vendor_or_payee": (description or "")[:255],
        "description": (description or "").strip(),
        "amount": amount,
        "payment_method": "Unknown",
        "w9_on_file": False,
        "tx_hash": compute_hash(tx_date, amount, description),
    }


# ---------------------------------------------------------------------------
# Filtering + persistence
# ---------------------------------------------------------------------------

def classify_for_preview(rows: list[dict[str, Any]], conn) -> dict[str, Any]:
    """Given parsed rows, mark each as 'internal_transfer', 'duplicate',
    'website_payout', or 'new'. Does not write anything; the preview UI uses
    this to let the owner override tags and decide what to import.
    """
    if not rows:
        return {"rows": rows, "counts": _count(rows)}

    existing_hashes = _fetch_existing_hashes(conn, [r["tx_hash"] for r in rows])
    payouts = _fetch_payments(conn, [r["tx_date"] for r in rows])

    for row in rows:
        if is_internal_transfer(row["description"]):
            row["skip_reason"] = "internal_transfer"
            continue
        if row["tx_hash"] in existing_hashes:
            row["skip_reason"] = "duplicate"
            continue
        if _matches_payout(row, payouts):
            row["skip_reason"] = "website_payout"
            continue
        row["skip_reason"] = None

    return {"rows": rows, "counts": _count(rows)}


def _count(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "new": sum(1 for r in rows if not r.get("skip_reason")),
        "duplicate": sum(1 for r in rows if r.get("skip_reason") == "duplicate"),
        "internal_transfer": sum(1 for r in rows if r.get("skip_reason") == "internal_transfer"),
        "website_payout": sum(1 for r in rows if r.get("skip_reason") == "website_payout"),
    }


def _fetch_existing_hashes(conn, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tx_hash FROM transactions WHERE tx_hash = ANY(%s)",
                (list(hashes),),
            )
            return {row[0] for row in cur.fetchall()}
    except Exception:
        # Table may not exist yet (fresh database before ensure_schema).
        return set()


def _fetch_payments(conn, dates: list[date]) -> list[tuple[date, Decimal]]:
    """Return (date, amount_cents->dollars) pairs for payments on the given
    dates. The payments table stores cents; the bank CSV stores dollars.
    """
    if not dates:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_at::date AS pdate, amount_cents
                FROM payments
                WHERE created_at::date = ANY(%s)
                """,
                (list(dates),),
            )
            return [(row[0], Decimal(row[1]) / Decimal(100)) for row in cur.fetchall()]
    except Exception:
        return []


def _matches_payout(row: dict[str, Any], payouts: list[tuple[date, Decimal]]) -> bool:
    """A bank deposit that matches a payments row by amount and date is the
    payout from Stripe/Zoho, not new income. Match on abs(amount) because the
    bank shows the deposit as a positive and the payments table stores cents.
    """
    row_abs = abs(row["amount"])
    row_date = row["tx_date"]
    for p_date, p_amount in payouts:
        if p_date == row_date and abs(p_amount) == row_abs:
            return True
    return False


def save_rows(conn, rows: list[dict[str, Any]], *, forced_tags: dict[str, str] | None = None) -> int:
    """Persist rows whose ``skip_reason`` is falsy. ``forced_tags`` maps a
    row's ``tx_hash`` to an owner-chosen business_unit override. Returns the
    number of rows actually inserted (duplicates during the INSERT are caught
    by the unique index and silently skipped).
    """
    forced_tags = forced_tags or {}
    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            if row.get("skip_reason"):
                continue
            override = forced_tags.get(row["tx_hash"])
            unit = override or row["business_unit"]
            try:
                cur.execute(
                    """
                    INSERT INTO transactions
                        (tx_date, business_unit, tx_type, vendor_or_payee,
                         description, amount, payment_method, w9_on_file, tx_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tx_hash) DO NOTHING
                    """,
                    (
                        row["tx_date"],
                        unit,
                        row["tx_type"],
                        row["vendor_or_payee"],
                        row["description"],
                        row["amount"],
                        row["payment_method"],
                        row["w9_on_file"],
                        row["tx_hash"],
                    ),
                )
                if cur.rowcount:
                    inserted += cur.rowcount
            except Exception:
                continue
    conn.commit()
    return inserted
