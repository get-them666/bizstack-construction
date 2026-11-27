#!/usr/bin/env python3
"""Load a DNC scrub download and keep only the numbers we might dial.

Why this exists
---------------
The FTC/registry download is the list of every registered number, often
hundreds of thousands of rows. Suppressing all of them would bloat
``suppression_list`` and take hours; only numbers that appear on our open leads
can ever be dialled by this app. This intersects the download with those
numbers, imports the matches, and records the scrub date so
``outbound_campaign.cold_calling_allowed`` unlocks for 31 days.

Input is a CSV/TXT (phone in the first column, one per line) or a ZIP of them.
``-`` reads the file from stdin, which is how a download is loaded inside the
production container without copying customer data around.

    python scripts/load_dnc_scrub.py ~/Downloads/dnc.zip
    python scripts/load_dnc_scrub.py ~/Downloads/dnc.zip --dry-run

Nothing is printed but counts: the file is a list of phone numbers.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_CLOSED = ("won", "lost", "closed", "cancelled", "canceled", "archived")


def last10(value: str) -> str:
    """Last ten digits of a US phone, or '' when the value is not one."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # US area codes start 2-9. A 10-digit account/timestamp that starts with
    # 0 or 1 must not be mistaken for a phone and suppressed.
    return digits if len(digits) == 10 and digits[0] in "23456789" else ""


def numbers_from_text(text: str) -> set[str]:
    """Every 10-digit phone in a CSV/TXT line, header rows ignored."""
    found: set[str] = set()
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        token = line.split(",")[0] if "," in line else line
        number = last10(token)
        if number:
            found.add(number)
            continue
        # Some registry exports put the phone in a later column.
        for field in line.split(","):
            number = last10(field)
            if number:
                found.add(number)
                break
    return found


def numbers_from_bytes(data: bytes) -> set[str]:
    """Read a CSV/TXT or a ZIP of them without extracting to disk."""
    if not data:
        return set()
    if zipfile.is_zipfile(io.BytesIO(data)):
        found: set[str] = set()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".csv", ".txt")):
                    found |= numbers_from_text(
                        archive.read(name).decode("utf-8", "replace"))
        return found
    return numbers_from_text(data.decode("utf-8", "replace"))


def lead_numbers(db) -> set[str]:
    """Distinct numbers on open construction leads — the only ones we can dial."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g') AS digits "
            "FROM leads WHERE company = 'construction' "
            "  AND lower(COALESCE(status, 'new')) <> ALL(%s) "
            "  AND length(regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g')) >= 10;",
            (list(_CLOSED),),
        )
        rows = cur.fetchall() or []
    numbers = set()
    for row in rows:
        raw = row.get("digits") if isinstance(row, dict) else row[0]
        number = last10(raw)
        if number:
            numbers.add(number)
    return numbers


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="CSV/TXT/ZIP of DNC numbers, or - for stdin")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the intersection without importing")
    parser.add_argument("--actor", default="dnc-download",
                        help="recorded on every suppression row")
    args = parser.parse_args(argv)

    try:
        if args.path == "-":
            data = sys.stdin.buffer.read()
        else:
            with open(os.path.expanduser(args.path), "rb") as fh:
                data = fh.read()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"could not read the file: {exc}"}))
        return 2

    scrub = numbers_from_bytes(data)
    if not scrub:
        print(json.dumps({"ok": False, "error": "no 10-digit phone numbers found"}))
        return 2

    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print(json.dumps({"ok": False, "error": "DATABASE_URL is not set"}))
        return 2

    import psycopg
    from psycopg.rows import dict_row

    import outbound_campaign

    with psycopg.connect(url, row_factory=dict_row) as db:
        leads = lead_numbers(db)
        matches = sorted(leads & scrub)
        if args.dry_run:
            print(json.dumps({
                "ok": True, "dry_run": True, "scrub_phones": len(scrub),
                "lead_phones": len(leads), "dnc_matches": len(matches),
            }))
            return 0

        fd, path = tempfile.mkstemp(prefix="dnc-matches-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for number in matches:
                    fh.write(f"+1{number}\n")
            result = outbound_campaign.import_dnc(db, path, actor=args.actor)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    print(json.dumps({
        **result, "scrub_phones": len(scrub), "lead_phones": len(leads),
        "dnc_matches": len(matches),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
