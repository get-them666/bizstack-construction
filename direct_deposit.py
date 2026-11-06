"""Direct deposit straight from the company's PNC business account -- no Stripe.

Crew enter routing + account in the phone app; the account number is stored
Fernet-encrypted (BANK_ENCRYPTION_KEY) and only the last four is ever shown
again. When a payroll run is finalized the owner downloads a NACHA PPD credit
file and uploads it to PNC's ACH origination (PINACLE); PNC pays each crew
member from the business account. The app never moves money itself.

PNC assigns the file-header identifiers when ACH origination is enabled on the
account. Until they are set the file still builds, and missing() names what is
blank so the owner knows what to ask PNC for:

  ACH_IMMEDIATE_DEST    PNC's routing number for the file header (9 digits)
  ACH_IMMEDIATE_ORIGIN  the originator id PNC assigns (10 chars; often '1'+EIN)
  ACH_COMPANY_ID        company identification in the batch header (10 chars)
  ACH_ODFI_ROUTING      first 8 digits of PNC's routing (trace numbers)
  ACH_COMPANY_NAME      up to 16 chars; defaults to the brand short name
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from cryptography.fernet import Fernet, InvalidToken

ACH_ENV = ("ACH_IMMEDIATE_DEST", "ACH_IMMEDIATE_ORIGIN", "ACH_COMPANY_ID", "ACH_ODFI_ROUTING")


class NotConfigured(Exception):
    pass


def enabled() -> bool:
    return bool((os.getenv("BANK_ENCRYPTION_KEY") or "").strip())


def _fernet() -> Fernet:
    key = (os.getenv("BANK_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise NotConfigured("BANK_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_bank_accounts (
                crew_id INTEGER PRIMARY KEY,
                account_holder VARCHAR(80) NOT NULL,
                routing_number VARCHAR(9) NOT NULL,
                account_last4 VARCHAR(4) NOT NULL,
                account_enc TEXT NOT NULL,
                account_type VARCHAR(10) NOT NULL DEFAULT 'checking',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS bank_method VARCHAR(10);")
        cur.execute("ALTER TABLE payroll_runs ADD COLUMN IF NOT EXISTS ach_sent_at TIMESTAMPTZ;")
        # Which file the owner actually downloaded, and when. A rejected upload
        # or a bank return can only be investigated if we can reproduce the
        # exact bytes that were sent, and "Mark sent" should not be clickable
        # before a file exists.
        cur.execute("ALTER TABLE payroll_runs ADD COLUMN IF NOT EXISTS nacha_downloaded_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE payroll_runs ADD COLUMN IF NOT EXISTS nacha_sha256 VARCHAR(64);")
    conn.commit()


def valid_routing(r: str) -> bool:
    """ABA routing number: 9 digits with the 3-7-1 checksum."""
    if not (r and r.isdigit() and len(r) == 9):
        return False
    w = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(int(d) * m for d, m in zip(r, w)) % 10 == 0


def save(conn, crew_id: int, holder: str, routing: str, account: str, account_type: str) -> dict:
    holder = (holder or "").strip()[:80]
    routing = "".join(ch for ch in (routing or "") if ch.isdigit())
    account = "".join(ch for ch in (account or "") if ch.isdigit())
    account_type = "savings" if (account_type or "").lower().startswith("s") else "checking"
    if len(holder) < 2:
        return {"ok": False, "error": "Enter the name on the account."}
    if not valid_routing(routing):
        return {"ok": False, "error": "That routing number isn't valid. It's the 9-digit number on the bottom-left of a check."}
    if not (4 <= len(account) <= 17):
        return {"ok": False, "error": "Account number should be 4 to 17 digits."}
    enc = _fernet().encrypt(account.encode()).decode()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO crew_bank_accounts (crew_id, account_holder, routing_number, account_last4, account_enc, account_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (crew_id) DO UPDATE SET
                account_holder = EXCLUDED.account_holder, routing_number = EXCLUDED.routing_number,
                account_last4 = EXCLUDED.account_last4, account_enc = EXCLUDED.account_enc,
                account_type = EXCLUDED.account_type, updated_at = now();
        """, (crew_id, holder, routing, account[-4:], enc, account_type))
        cur.execute("UPDATE crew SET bank_status = 'connected', bank_method = 'ach' WHERE id = %s;", (crew_id,))
    conn.commit()
    return {"ok": True, **masked(conn, crew_id)}


def masked(conn, crew_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT account_holder, routing_number, account_last4, account_type, updated_at "
                    "FROM crew_bank_accounts WHERE crew_id = %s;", (crew_id,))
        row = cur.fetchone()
    if not row:
        return {"on_file": False}
    return {"on_file": True, "account_holder": row["account_holder"], "routing_number": row["routing_number"],
            "account_last4": row["account_last4"], "account_type": row["account_type"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}


def remove(conn, crew_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM crew_bank_accounts WHERE crew_id = %s;", (crew_id,))
        cur.execute("UPDATE crew SET bank_status = 'none', bank_method = NULL WHERE id = %s AND bank_method = 'ach';", (crew_id,))
    conn.commit()


def _decrypt(enc: str) -> str:
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken as e:
        raise NotConfigured("BANK_ENCRYPTION_KEY does not match the stored data") from e


def missing() -> list[str]:
    return [k for k in ACH_ENV if not (os.getenv(k) or "").strip()]


def run_lines(conn, run_id: int) -> list[dict]:
    """Payroll lines for a run with decrypted bank details; lines with no bank on file are flagged."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pl.crew_id, pl.gross_cents, c.name,
                   b.account_holder, b.routing_number, b.account_enc, b.account_type
              FROM payroll_lines pl
              JOIN crew c ON c.id = pl.crew_id
              LEFT JOIN crew_bank_accounts b ON b.crew_id = pl.crew_id
             WHERE pl.run_id = %s AND COALESCE(pl.gross_cents, 0) > 0
             ORDER BY c.name;
        """, (run_id,))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["has_bank"] = bool(r.get("account_enc"))
        r["account_number"] = _decrypt(r.pop("account_enc")) if r["has_bank"] else ""
    return rows

def _next_business_day(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _f(s, n, left=True):
    s = (s or "")
    s = "".join(ch for ch in str(s) if 32 <= ord(ch) < 127).upper()[:n]
    return s.ljust(n) if left else s.rjust(n, "0")


def validate_nacha(text: str) -> list[str]:
    """Structural check of a generated NACHA file. Returns problems, empty is good.

    This is the guard that would have caught the original header bug: the
    Immediate Destination field is a *blank* followed by the 9-digit routing
    number, not a zero-padded 10-digit number. PNC rejects the whole file for
    that, and the owner only finds out days later when nobody was paid.
    """
    problems: list[str] = []
    recs = [r for r in (text or "").replace("\r\n", "\n").split("\n") if r]
    if not recs:
        return ["file is empty"]
    for i, r in enumerate(recs):
        if len(r) != 94:
            problems.append(f"record {i + 1} is {len(r)} characters, not 94")
    if problems:
        return problems
    if recs[0][0] != "1":
        problems.append("first record is not a file header")
    dest = recs[0][3:13]
    if not (dest.startswith(" ") and dest[1:].isdigit()):
        problems.append("immediate destination must be a blank followed by the 9-digit routing number")
    elif not valid_routing(dest[1:]):
        problems.append("immediate destination routing number fails the ABA checksum")

    batch_headers = [i for i, r in enumerate(recs) if r[0] == "5"]
    batch_controls = [i for i, r in enumerate(recs) if r[0] == "8"]
    if not batch_headers or not batch_controls:
        problems.append("missing batch header or batch control record")
        return problems

    entries = [r for r in recs[batch_headers[0] + 1:batch_controls[0]] if r[0] == "6"]
    n = len(entries)
    total = sum(int(r[29:39]) for r in entries)
    entry_hash = sum(int(r[3:11]) for r in entries) % 10_000_000_000
    ctrl = recs[batch_controls[0]]
    if int(ctrl[4:10]) != n:
        problems.append(f"batch control says {int(ctrl[4:10])} entries, file has {n}")
    if int(ctrl[10:20]) != entry_hash:
        problems.append("batch control entry hash does not match the entries")
    if int(ctrl[32:44]) != total:
        problems.append("batch control total credit does not match the entries")

    file_ctrl = next((r for r in recs if r[0] == "9" and r[1:7].strip().isdigit()), None)
    if file_ctrl is None:
        problems.append("missing file control record")
    else:
        if int(file_ctrl[13:21]) != n:
            problems.append(f"file control says {int(file_ctrl[13:21])} entries, file has {n}")
        if int(file_ctrl[43:55]) != total:
            problems.append("file control total credit does not match the entries")
        blocks = int(file_ctrl[7:13])
        if blocks != len(recs) // 10:
            problems.append(f"file control says {blocks} blocks, file has {len(recs) // 10}")
    return problems


def nacha(lines: list[dict], *, company_name: str, effective: date | None = None,
          created: datetime | None = None, description: str = "PAYROLL") -> tuple[str, dict]:
    """Build a NACHA PPD credit file. Returns (text, summary)."""
    created = created or datetime.now()
    effective = effective or _next_business_day(created.date())
    dest = "".join(ch for ch in (os.getenv("ACH_IMMEDIATE_DEST") or "") if ch.isdigit())[:9]
    origin = (os.getenv("ACH_IMMEDIATE_ORIGIN") or "").strip()[:10]
    company_id = (os.getenv("ACH_COMPANY_ID") or "").strip()[:10]
    odfi = "".join(ch for ch in (os.getenv("ACH_ODFI_ROUTING") or dest) if ch.isdigit())[:8]
    cname = (os.getenv("ACH_COMPANY_NAME") or company_name or "")[:16]
    dest_name = (os.getenv("ACH_DEST_NAME") or "PNC BANK")[:23]

    paid = [l for l in lines if l.get("has_bank")]
    skipped = [l["name"] for l in lines if not l.get("has_bank")]

    rec = []
    
    # 1 Record: File Header Record
    # Immediate Destination is 10 positions: a blank followed by the 9-digit
    # routing number. Zero-padding it to 10 digits (the old code did) is not a
    # valid NACHA destination, and PNC rejects the whole file for it.
    dest_field = (" " + dest) if len(dest) == 9 else _f(dest, 10)
    rec.append("1" + "01" + dest_field + _f(origin, 10)
               + created.strftime("%y%m%d") + created.strftime("%H%M") + "A" + "094" + "10" + "1"
               + _f(dest_name, 23) + _f(cname, 23) + _f("", 8))
               
    batch_no = 1
    # 5 Record: Batch Header Record
    rec.append("5" + "220" + _f(cname, 16) + _f("", 20) + _f(company_id, 10) + "PPD"
               + _f(description, 10) + _f(effective.strftime("%b%d").upper(), 6) + effective.strftime("%y%m%d")
               + _f("", 3) + "1" + _f(odfi, 8) + _f(str(batch_no), 7, left=False))
               
    entry_hash = 0
    total = 0
    
    # 6 Record: Entry Detail Record Processing Loop
    for i, l in enumerate(paid, start=1):
        rdfi, chk = l["routing_number"][:8], l["routing_number"][8]
        entry_hash += int(rdfi)
        amt = int(l["gross_cents"])
        total += amt
        tc = "32" if l.get("account_type") == "savings" else "22"
        rec.append("6" + tc + rdfi + chk + _f(l["account_number"], 17) + _f(str(amt), 10, left=False)
                   + _f(str(l["crew_id"]), 15) + _f(l.get("account_holder") or l["name"], 22) + _f("", 2) + "0"
                   + _f(odfi, 8) + _f(str(i), 7, left=False))
                   
    eh = _f(str(entry_hash)[-10:], 10, left=False)
    n = len(paid)
    
    # 8 Record: Batch Control Record
    rec.append("8" + "220" + _f(str(n), 6, left=False) + eh + _f("0", 12, left=False) + _f(str(total), 12, left=False)
               + _f(company_id, 10) + _f("", 19) + _f("", 6) + _f(odfi, 8) + _f(str(batch_no), 7, left=False))
               
    # 9 Record: Core Structural Placeholder Insertion
    rec.append("PLACEHOLDER_FOR_RECORD_9")
    
    # Structural Loop Block Padding (Fixed: standard "9" followed by 93 structural spaces)
    while len(rec) % 10:
        rec.append("9" + _f("", 93))
        
    # Calculate structural blocks precisely (1 block = 10 full lines)
    blocks = len(rec) // 10
    
    # Overwrite the structural metadata row placeholder with calculated file variables
    rec[n + 3] = ("9" + _f("1", 6, left=False) + _f(str(blocks), 6, left=False) + _f(str(n), 8, left=False) + eh
                  + _f("0", 12, left=False) + _f(str(total), 12, left=False) + _f("", 39))
                  
    assert all(len(r) == 94 for r in rec), [len(r) for r in rec]
    text = "\r\n".join(rec) + "\r\n"
    return text, {"entries": n, "total_cents": total, "skipped": skipped, "effective": effective.isoformat(),
                  "missing_env": missing(), "problems": validate_nacha(text)}


def ach_csv(lines: list[dict]) -> str:
    """Plain CSV of the same payments, for banks whose portal imports CSV instead of NACHA."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name on account", "Routing number", "Account number", "Account type", "Amount"])
    for l in lines:
        if l.get("has_bank"):
            w.writerow([l.get("account_holder") or l["name"], l["routing_number"], l["account_number"],
                        l.get("account_type") or "checking", f"{int(l['gross_cents'])/100:.2f}"])
    return buf.getvalue()
