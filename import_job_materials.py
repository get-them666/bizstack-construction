"""Import this job's materials from a CSV into the BuildStack owner price book.

These land on /materials and become selectable for quotes. Prices come straight
from your CSV — treat them as final.

CSV columns (header optional; order flexible if header present):
    name, price, store, brand, category, sku, model, unit, notes

Usage:
    cd ~/bizstack-construction
    DATABASE_URL=postgresql://... venv/bin/python import_job_materials.py path/to/file.csv
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import materials_service as ms

_BID_COMPANY = "construction"
EXPECTED = ["name", "price", "store", "brand", "category", "sku", "model", "unit", "notes"]


def _db():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: set DATABASE_URL before running", file=sys.stderr)
        sys.exit(1)
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row)


def _row_to_dict(cells, header):
    """Map one row to a dict using header names, or positional fallback."""
    if header:
        out = {}
        for col in EXPECTED:
            idx = header.get(col)
            out[col] = cells[idx] if idx is not None and idx < len(cells) else ""
        return out
    # Positional fallback: name, price, store, brand, category, sku, model, unit, notes
    out = {col: "" for col in EXPECTED}
    for i, col in enumerate(EXPECTED):
        if i < len(cells):
            out[col] = cells[i]
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: import_job_materials.py path/to/file.csv", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    conn = _db()
    try:
        ms.seed_materials_catalog(conn)

        imported = 0
        skipped = 0
        errors = []

        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            rows = list(reader)

        # Detect header row
        header = None
        data_start = 0
        if rows:
            first_low = [c.strip().lower() for c in rows[0]]
            if any(col in first_low for col in ("name", "item", "description")):
                header = {name: i for i, name in enumerate(first_low) if name in EXPECTED}
                data_start = 1

        for lineno, cells in enumerate(rows[data_start:], start=data_start + 1):
            line = [c.strip().strip('"') for c in cells]
            if not any(line):
                skipped += 1
                continue

            r = _row_to_dict(line, header)
            name = r["name"]
            price = r["price"]
            if not name or price == "":
                skipped += 1
                continue

            res = ms.upsert_catalog_item(
                conn,
                name=name,
                price_dollars=price,
                store=r["store"],
                brand=r["brand"],
                category=r["category"],
                model=r["model"],
                sku=r["sku"],
                unit=r["unit"] or "each",
                notes=r["notes"],
            )
            if res.get("ok"):
                imported += 1
                print(f"  OK   {name:40} ${price:>7}  id={res['id']}")
            else:
                errors.append(f"line {lineno}: {name} — {res.get('error')}")
                print(f"  FAIL {name:40} {res.get('error')}")

        print(f"\n{imported} materials imported into materials_catalog.")
        if skipped:
            print(f"{skipped} blank/incomplete rows skipped.")
        if errors:
            print(f"{len(errors)} errors:")
            for e in errors:
                print("   ", e)
        print("\nThey now appear on /materials (owner price book) and can be used in quotes.")
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()
