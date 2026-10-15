"""Itemized material takeoff, priced at cost, for the owner's own use.

The customer picker shows public shelf prices. This is the other half: what a
job actually costs *us*, line by line, so it can be reconciled against the
invoice and handed to the accountant.

DISTINCTION THAT MATTERS
------------------------
Prices here come from every supplier including account-specific ones, because
this module is only ever called from behind require_admin. There is no public
entry point and no parameter that would let one be built. material_picker is
the public half; this is not, and the two must not be merged.

Costs resolve in this order, most authoritative first:
  1. ABC Supply, priced per account for the real quantity -- the number that
     actually appears on the invoice.
  2. A yard's price sheet, if that yard stocks the item.
  3. Nothing. A missing cost stays missing and is reported as a gap rather
     than estimated, because a takeoff that quietly fills a hole with a guess
     produces a job cost that is wrong by an unknown amount.

That last rule is the point of the whole module.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

import abc_supply
import supplier_prices as sp

# Categories we can group a takeoff by. Free-text so an unforeseen trade still
# lands somewhere rather than being forced into a wrong bucket.
GROUPS = ["demo", "framing", "roofing", "drywall", "flooring", "paint",
          "millwork", "sitework", "electrical", "plumbing", "hvac",
          "fasteners", "safety", "cleanup", "other"]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        # No inline REFERENCES here. bid_opportunities is created by its own
        # module, and an inline FK makes this table's creation fail outright if
        # that has not run yet -- which turns a module load order into a boot
        # failure. The FK is added below, guarded, the same way bid_costing
        # handles projects.bid_id.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS takeoff_sheets (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER,
                name VARCHAR(255) NOT NULL,
                company VARCHAR(40) DEFAULT 'construction',
                markup_pct NUMERIC(6,2) DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE takeoff_sheets ADD COLUMN IF NOT EXISTS bid_id INTEGER;")
        cur.execute("SELECT to_regclass('bid_opportunities') AS t")
        if cur.fetchone()["t"]:
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s "
                        "AND conrelid = 'takeoff_sheets'::regclass",
                        ("takeoff_sheets_bid_id_fkey",))
            if not cur.fetchone():
                cur.execute("ALTER TABLE takeoff_sheets ADD CONSTRAINT "
                            "takeoff_sheets_bid_id_fkey FOREIGN KEY (bid_id) "
                            "REFERENCES bid_opportunities(id) ON DELETE CASCADE")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_takeoff_sheets_bid ON takeoff_sheets(bid_id);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS takeoff_lines (
                id SERIAL PRIMARY KEY,
                sheet_id INTEGER NOT NULL REFERENCES takeoff_sheets(id) ON DELETE CASCADE,
                item_number VARCHAR(80) NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                uom VARCHAR(40) DEFAULT 'each',
                qty NUMERIC(12,3) NOT NULL DEFAULT 1,
                unit_cost_cents BIGINT NOT NULL DEFAULT 0,
                supplier VARCHAR(120),
                cost_source VARCHAR(30) DEFAULT 'book',
                group_name VARCHAR(60) DEFAULT 'other',
                note TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_takeoff_lines_sheet ON takeoff_lines(sheet_id);")
    conn.commit()


# --- sheets ------------------------------------------------------------------
def create_sheet(conn, name: str, bid_id: int | None = None,
                 markup_pct: float = 0.0, notes: str = "",
                 company: str = "construction") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO takeoff_sheets (name, bid_id, markup_pct, notes, company) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name.strip()[:255], bid_id, markup_pct, (notes or "")[:2000] or None, company))
        sid = cur.fetchone()["id"]
    conn.commit()
    return sid


def get_sheet(conn, sheet_id: int, company: str = "construction") -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM takeoff_sheets WHERE id = %s AND company = %s",
                    (sheet_id, company))
        sheet = cur.fetchone()
        if not sheet:
            return None
        cur.execute("SELECT * FROM takeoff_lines WHERE sheet_id = %s ORDER BY sort_order, id",
                    (sheet_id,))
        sheet["lines"] = cur.fetchall()
    return sheet


def list_sheets(conn, bid_id: int | None = None,
                company: str = "construction") -> list[dict]:
    sql = ["SELECT * FROM takeoff_sheets WHERE company = %s"]
    params: list = [company]
    if bid_id is not None:
        sql.append("AND bid_id = %s")
        params.append(bid_id)
    sql.append("ORDER BY created_at DESC")
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        return cur.fetchall()


def delete_sheet(conn, sheet_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM takeoff_sheets WHERE id = %s AND company = %s RETURNING id",
                    (sheet_id, company))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


# --- lines -------------------------------------------------------------------
def add_line(conn, sheet_id: int, description: str, qty: float = 1,
             item_number: str = "", uom: str = "each", unit_cost_cents: int | None = None,
             supplier: str = "", cost_source: str = "book", group_name: str = "other",
             note: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO takeoff_lines (sheet_id, item_number, description, uom, qty, "
            "unit_cost_cents, supplier, cost_source, group_name, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (sheet_id, (item_number or "")[:80], description[:2000], (uom or "each")[:40],
             qty, int(unit_cost_cents or 0), (supplier or "")[:120] or None,
             cost_source, group_name, (note or "")[:1000] or None))
        lid = cur.fetchone()["id"]
    conn.commit()
    return lid


def delete_line(conn, line_id: int, sheet_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM takeoff_lines WHERE id = %s AND sheet_id = %s RETURNING id",
                    (line_id, sheet_id))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


def set_line_cost(conn, line_id: int, unit_cost_cents: int, supplier: str = "",
                  cost_source: str = "manual") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE takeoff_lines SET unit_cost_cents = %s, supplier = %s, "
            "cost_source = %s WHERE id = %s RETURNING id",
            (int(unit_cost_cents), supplier or None, cost_source, line_id))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


# --- costing -----------------------------------------------------------------
def totals(sheet: dict) -> dict:
    """Extended cost, grouped, and the price the customer would be shown.

    `missing_cost` counts lines priced at zero. Those are the lines that make
    a takeoff untrustworthy, so they are surfaced rather than netted out.
    """
    lines = sheet.get("lines") or []
    total = 0
    missing = 0
    groups: dict[str, dict] = {}
    for ln in lines:
        qty = float(ln["qty"] or 0)
        unit = int(ln["unit_cost_cents"] or 0)
        ext = int(round(unit * qty))
        if unit <= 0:
            missing += 1
        total += ext
        g = ln["group_name"] or "other"
        slot = groups.setdefault(g, {"count": 0, "cost": 0})
        slot["count"] += 1
        slot["cost"] += ext
    markup = float(sheet.get("markup_pct") or 0)
    sell = total * (1 + markup)
    return {
        "lines": len(lines),
        "cost_total": total,
        "missing_cost": missing,
        "markup_pct": markup,
        "sell_total": int(round(sell)),
        "expected_margin": int(round(sell - total)),
        "by_group": sorted(groups.items(), key=lambda kv: -kv[1]["cost"]),
        "complete": missing == 0,
    }


def cost_lookup(conn, item_numbers: list[str], company: str = "construction") -> dict:
    """Best available cost per item, from the private price book.

    ABC first, because an account price is what lands on the invoice.
    """
    out: dict[str, dict] = {}
    if not item_numbers:
        return out
    by_item = sp.price_item_numbers(conn, item_numbers, company)
    for sku, suppliers in by_item.items():
        best = None
        for name, info in suppliers.items():
            cents = int(info["price_cents"] or 0)
            if cents <= 0:
                continue
            if best is None or cents < best["unit_cost_cents"]:
                best = {"unit_cost_cents": cents, "supplier": name,
                        "cost_source": "book"}
        if best:
            out[sku] = best
    return out


def abc_price(items: list[dict], purpose: str = "estimating") -> list[dict]:
    """Live ABC Supply prices for the given items, at the real quantities.

    Kept separate from cost_lookup because it is a network call and should
    only happen when the owner asks for it, not on every render.
    """
    if not abc_supply.configured():
        raise ValueError("ABC Supply is not configured; missing " + ", ".join(abc_supply.missing()))
    return abc_supply.price_items(items, purpose=purpose)


# --- export ------------------------------------------------------------------
ACCOUNTING_HEADERS = ["Date", "Ref", "Vendor", "Account", "Item", "Description",
                      "Qty", "Rate", "Amount"]


def accounting_csv(sheet: dict, vendor: str = "", ref: str = "",
                   when: str = "") -> str:
    """Itemized cost as a CSV shaped for import into accounting software.

    One row per takeoff line, account coded as Materials:Inventory so it
    lands where it belongs rather than as generic expense. Vendor comes from
    the line where known, so multi-vendor takeoffs stay attributable per line.

    This is the reconciliation file: cost of materials for a job, itemized,
    ready to drop in alongside the invoice.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ACCOUNTING_HEADERS)
    day = when or date.today().isoformat()
    for ln in sheet.get("lines") or []:
        qty = float(ln["qty"] or 0)
        unit = int(ln["unit_cost_cents"] or 0)
        ext = int(round(unit * qty))
        w.writerow([day, ref, ln["supplier"] or vendor or "", "Materials:Inventory",
                    ln["item_number"] or "", ln["description"], qty,
                    f"{unit / 100:.2f}", f"{ext / 100:.2f}"])
    return buf.getvalue()


def bid_cost_csv(conn, bid_id: int, company: str = "construction") -> str:
    """Everything the accountant needs for one bid: takeoff plus entered costs."""
    sheets = list_sheets(conn, bid_id, company)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bid_costs WHERE bid_id = %s AND company = %s ORDER BY category, id",
                    (bid_id, company))
        cost_lines = cur.fetchall()
        cur.execute("SELECT name, reference, status, bid_amount, contract_value, "
                    "won_date, lost_date FROM bid_opportunities WHERE id = %s AND company = %s",
                    (bid_id, company))
        bid = cur.fetchone()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ACCOUNTING_HEADERS)
    day = (bid or {}).get("won_date") or date.today().isoformat()
    day = day.isoformat() if hasattr(day, "isoformat") else str(day)
    ref = ((bid or {}).get("reference") or f"bid-{bid_id}")[:80]

    for sh in sheets:
        full = get_sheet(conn, sh["id"], company)
        for ln in full["lines"]:
            qty = float(ln["qty"] or 0)
            unit = int(ln["unit_cost_cents"] or 0)
            w.writerow([day, ref, ln["supplier"] or "", "Materials:Inventory",
                        ln["item_number"] or "", ln["description"], qty,
                        f"{unit / 100:.2f}", f"{int(round(unit * qty)) / 100:.2f}"])
    for cl in cost_lines:
        qty = float(cl["qty"] or 1)
        unit = float(cl["unit_cost"] or 0)
        acct = {"labor": "Labor", "material": "Materials:Inventory",
                "sub": "Subcontractors", "equipment": "Equipment",
                "permit": "Permits & Fees"}.get(cl["category"], "Other")
        w.writerow([day, ref, cl["vendor"] or "", acct, "",
                    cl["description"] or cl["category"], qty,
                    f"{unit / 100:.2f}", f"{(cl['actual'] or cl['estimated'] or 0) / 100:.2f}"])
    return buf.getvalue()


def summary_for_bids(conn, bid_ids: list[int], company: str = "construction") -> dict:
    """Material cost roll-up across several jobs, for a quick statistics read."""
    out = {}
    for bid_id in bid_ids:
        sheets = list_sheets(conn, bid_id, company)
        agg = {"cost_total": 0, "sheets": len(sheets), "missing_cost": 0, "lines": 0}
        for sh in sheets:
            t = totals(get_sheet(conn, sh["id"], company))
            agg["cost_total"] += t["cost_total"]
            agg["missing_cost"] += t["missing_cost"]
            agg["lines"] += t["lines"]
        out[bid_id] = agg
    return out