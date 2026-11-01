"""Create this job's bid card in BuildStack Construction, priced from the
materials catalogue.

What it does, in order:

1. Ensures the bid + costing schemas exist.
2. Looks up every material by exact catalogue name and reads its price_cents.
   Nothing is hardcoded: the prices are whatever was imported from the supplier
   CSVs. A missing item fails loudly instead of silently pricing at zero.
3. Creates one ``bid_opportunities`` row (reference ``hall-runner-bath-2026-10``,
   so re-running does not create a second card).
4. Writes one ``bid_costs`` row per line: ``unit_cost`` is what the line costs
   us, ``estimated`` is the client price (cost x PRICE_MULTIPLIER).
5. Sets ``bid_amount`` to the sum of the client prices.

Run a dry run first -- it prints the whole card and writes nothing:

    python seed_this_job.py --dry-run

Then, against the real database:

    python seed_this_job.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Also allow running the file from outside the repo (e.g. copied into a
# container's /tmp) as long as the working directory is the repo.
sys.path.insert(0, os.getcwd())

import bid_costing as bc
import bid_opportunities as bo
import psycopg
from psycopg.rows import dict_row

COMPANY = "construction"
REFERENCE = "hall-runner-bath-2026-10"

# Client price per line. Labor is billed above the crew's $40/hr cost -- that
# gap is where the money is on a small job, not in a 25% markup on a $15 sheet
# of drywall. Materials and allowances carry 50%, which is normal for retail
# residential work.
#
# The crew rate was carried at $50/hr until 2026-10-31; $40/hr is the actual
# agreed wage. The billed rate did not move with it, so the difference lands in
# margin rather than in the client's price.
LABOR_COST_RATE = 40.0
LABOR_BILLED_RATE = 72.0
MATERIAL_MARKUP = 1.50
SUB_MARKUP = 1.50
OTHER_MARKUP = 1.50

# Concealed conditions are the whole risk on this job: nobody knows what the
# sewage did to the subfloor until it comes up. Priced as a line, not absorbed.
CONTINGENCY_PCT = 10.0

# ── the job ────────────────────────────────────────────────────────────────
# (description, category, qty, unit, hours, catalog_name)
#   catalog_name set  -> material line, unit_cost comes from materials_catalog
#   catalog_name None -> labour line, unit_cost = hours x LABOR_RATE
#   catalog_name ""   -> allowance line, priced at `qty` x `hours` (see ALLOWANCES)
#
# Descriptions are written the way they should read on the client's bid, not
# the way they are entered in the catalogue.
LINES = [
    # --- sewage cleanup ---------------------------------------------------
    ("Sewage cleanup, disinfection and haul-away", "other", 10, "hr", 10, None),
    ("Disinfectant, PPE and disposal supplies", "material", 1, "allowance", 85, ""),
    ("Debris disposal and dump fees", "other", 1, "allowance", 120, ""),

    # --- plumbing ---------------------------------------------------------
    ("Toilet plumbing: remove, reset, supply and drain connections", "sub", 6, "hr", 6, None),
    ("Toilet plumbing materials: flange, wax ring, supply line, PVC fittings", "sub", 1, "allowance", 75, ""),

    # --- bathroom floor ---------------------------------------------------
    ("Bathroom floor demolition and subfloor preparation", "other", 8, "hr", 8, None),
    ("Subfloor, 3/4 in. OSB tongue and groove", "material", 2, "sheet", 0, "3/4 in. 4x8 OSB Tongue & Groove Subfloor"),
    ("Bathroom flooring, waterproof LVP, supplied and installed", "material", 40, "sqft", 0, "Luxury Vinyl Plank Waterproof 6x48"),
    ("Bathroom flooring installation", "labor", 4, "hr", 4, None),

    # --- drywall ----------------------------------------------------------
    ("Drywall, 1/2 in. 4x8 sheets", "material", 6, "sheet", 0, "1/2 in. 4x8 Drywall Sheet"),
    ("Moisture-resistant drywall, 1/2 in. 4x8 (bathroom)", "material", 2, "sheet", 0, "1/2 in. 4x8 Mold & Moisture Resistant Drywall"),
    ("Joint compound, 5 gal", "material", 1, "bucket", 0, "Ready-Mix Joint Compound 5 Gal"),
    ("Drywall tape, 250 ft", "material", 1, "roll", 0, "Standard Fibermesh Drywall Joint Tape 250ft"),
    ("Drywall screws, 5 lb", "material", 1, "box", 0, "Drywall Screws 1-1/4 in 5 lb"),
    ("Corner bead, 8 ft", "material", 4, "each", 0, "Corner Bead 8 ft"),
    ("Drywall installation and finishing: hang, tape, mud, sand", "labor", 16, "hr", 16, None),
    ("Drywall priming", "labor", 2, "hr", 2, None),
    ("Primer, 1 gal", "material", 1, "gal", 0, "Primer Interior/Exterior 1 Gal"),

    # --- molding ----------------------------------------------------------
    ("Baseboard molding, primed MDF 3-1/4 in., supplied and installed", "material", 44, "lf", 0, "WM623 3-1/4 in. Primed MDF Baseboard Trim"),
    ("Door casing, primed MDF 2-1/4 in., 2 doors", "material", 34, "lf", 0, "WM356 2-1/4 in. Primed MDF Casing Trim"),
    ("Molding installation: baseboard and casing", "labor", 8, "hr", 8, None),

    # --- laminate runner --------------------------------------------------
    ("Laminate flooring, 8mm AC4, runner area, supplied and installed", "material", 40, "sqft", 0, "Laminate Flooring 8mm AC4"),
    ("Laminate flooring installation, runner area", "labor", 4, "hr", 4, None),

    # --- paint ------------------------------------------------------------
    ("Interior paint, flat/eggshell, hallway and bathroom walls", "material", 2, "gal", 0, "Interior Paint Flat 1 Gal Eggshell"),
    ("Interior paint, satin, doors and trim", "material", 1, "gal", 0, "Interior Paint Satin 1 Gal"),
    ("Painter's tape, 1.88 in. x 60 yd", "material", 2, "roll", 0, "Painter's Tape 1.88 in x 60 yd"),
    ("Paint roller kit, 9 in.", "material", 1, "kit", 0, "Paint Roller Kit 9 in"),
    ("Paintable silicone caulk, 10.1 oz", "material", 2, "tube", 0, "Caulk Paintable Silicone 10.1 oz"),
    ("Wall painting, two coats, hallway and bathroom", "labor", 14, "hr", 14, None),
    ("Door painting, two doors, two coats", "labor", 4, "hr", 4, None),

    # --- wrap up ----------------------------------------------------------
    ("Materials pickup and site protection", "labor", 3, "hr", 3, None),
    ("Final cleanup", "labor", 4, "hr", 4, None),
]

NOTES = """\
Hallway (8' x 4') and runner (8' x 4') plus bathroom. Interior finish work only.

Bathroom dimensions and subfloor condition are not confirmed. If demolition
exposes rot or a failed subfloor beyond the two sheets listed, that is a written
change order before work continues.

Allowance lines (disinfectant/PPE, dump fees, toilet plumbing materials) are
estimates to be reconciled against receipts at final billing. Trim is priced per
linear foot from the supplier sheet.

The contingency line covers concealed conditions found after demolition. Any
unused balance is credited at final billing.
"""

# One task per line, in the order the work happens. This is what prints under
# SCOPE OF WORK on the bid package -- the cost lines below are priced per unit
# and would read like a shopping list.
SCOPE = """\
Remove and haul away sewage-contaminated material; clean, disinfect and dry the affected areas.
Demolish the bathroom floor and prepare the subfloor; replace up to two sheets of 3/4 in. OSB.
Toilet plumbing: remove and reset the toilet, connect supply and drain, replace flange and wax ring.
Hang, tape, mud, sand and prime drywall: six 1/2 in. sheets plus two moisture-resistant sheets in the bathroom.
Install primed MDF baseboard, approximately 44 linear feet, and casing on two interior doors.
Install laminate flooring in the runner area, approximately 40 square feet, including transitions.
Install waterproof luxury vinyl plank flooring in the bathroom, approximately 40 square feet.
Paint hallway and bathroom walls, two finish coats over primed surfaces.
Paint two interior doors and the new trim, two coats.
Remove job debris, dispose of it, and leave the work areas broom clean.
"""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return psycopg.connect(url, row_factory=dict_row)


def _catalog_prices(conn, names: list[str]) -> dict[str, int]:
    """price_cents by exact name. Missing names are returned as absent."""
    if not names:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, price_cents FROM materials_catalog "
            "WHERE enabled AND name = ANY(%s)",
            (names,),
        )
        return {r["name"]: int(r["price_cents"] or 0) for r in cur.fetchall()}


def _set_unit(conn, line_id: int, unit: str) -> None:
    """Write the unit separately.

    bid_costing.add_cost_line gained a `unit` argument at the same time the
    column was added, but a container still running the previous deploy does not
    have it. Writing it directly keeps this script runnable against either.
    """
    if not unit:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE bid_costs SET unit = %s WHERE id = %s", (unit, line_id))
    conn.commit()


def build_rows(catalog: dict[str, int]) -> list[dict]:
    rows = []
    for desc, cat, qty, unit, hours, catalog_name in LINES:
        if catalog_name is None:                       # labour
            qty_eff = float(hours)
            unit = "hr"
            unit_cost = LABOR_COST_RATE
            cost = float(hours) * LABOR_COST_RATE
            price = float(hours) * LABOR_BILLED_RATE
        elif catalog_name == "":                       # allowance
            qty_eff = 1.0
            unit = "allowance"
            unit_cost = float(hours)
            cost = unit_cost * float(qty)
            price = cost * OTHER_MARKUP
        else:                                          # material
            if catalog_name not in catalog:
                raise SystemExit(f"missing catalogue item: {catalog_name!r}")
            unit_cost = catalog[catalog_name] / 100.0
            qty_eff = float(qty)
            cost = unit_cost * qty_eff
            markup = SUB_MARKUP if cat == "sub" else MATERIAL_MARKUP
            price = cost * markup
        rows.append({
            "description": desc,
            "category": cat,
            "qty": qty_eff,
            "unit": unit,
            "unit_cost": round(unit_cost, 2),
            "cost": round(cost, 2),
            "price": round(price, 2),
        })

    # Contingency is priced off everything else and carries no cost, so it lands
    # in the margin where it belongs until it is either used or released.
    base = sum(r["price"] for r in rows)
    if CONTINGENCY_PCT:
        rows.append({
            "description": "Contingency - concealed conditions after demolition",
            "category": "other",
            "qty": 1.0,
            "unit": "allowance",
            "unit_cost": 0.0,
            "cost": 0.0,
            "price": round(base * CONTINGENCY_PCT / 100.0, 2),
        })
    return rows


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    conn = _connect()
    try:
        bo.ensure_schema(conn)
        bc.ensure_accounting_schema(conn)
        # ensure_schema on a container that predates this change will not know
        # the column yet, and create_bid would silently drop the scope kwarg.
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bid_opportunities "
                        "ADD COLUMN IF NOT EXISTS scope TEXT;")
            cur.execute("ALTER TABLE bid_costs "
                        "ADD COLUMN IF NOT EXISTS unit VARCHAR(12);")
        conn.commit()

        names = [l[5] for l in LINES if l[5]]
        catalog = _catalog_prices(conn, names)
        missing = sorted({n for n in names if n not in catalog})
        if missing:
            print("ERROR: these catalogue items do not exist (or are disabled):")
            for m in missing:
                print("   ", m)
            sys.exit(1)

        rows = build_rows(catalog)
        subtotal_cost = sum(r["cost"] for r in rows)
        total_price = sum(r["price"] for r in rows)
        labor_hours = sum(l[4] for l in LINES if l[5] is None)

        print(f"{'category':9} {'description':62} {'cost':>10} {'price':>10}")
        print("-" * 96)
        for r in rows:
            print(f"{r['category']:9} {r['description'][:62]:62} "
                  f"{r['cost']:>10,.2f} {r['price']:>10,.2f}")
        print("-" * 96)
        print(f"{'':9} {'TOTAL':62} {subtotal_cost:>10,.2f} {total_price:>10,.2f}")
        print(f"\nLabour: {labor_hours} hrs, ${LABOR_COST_RATE:,.0f}/hr cost, "
              f"billed at ${LABOR_BILLED_RATE:,.0f}/hr")
        print(f"Margin built in: ${total_price - subtotal_cost:,.2f} "
              f"({(total_price - subtotal_cost) / total_price:.0%} of price)")

        if dry_run:
            print("\nDRY RUN - nothing written.")
            return

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM bid_opportunities "
                "WHERE company = %s AND reference = %s",
                (COMPANY, REFERENCE),
            )
            existing = cur.fetchone()
        if existing:
            print(f"\nBid card already exists: #{existing['id']} "
                  f"({existing['status']}) - not creating a duplicate.")
            if "--reprice" in sys.argv:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bid_costs WHERE bid_id = %s AND company = %s",
                        (existing["id"], COMPANY),
                    )
                conn.commit()
                for r in rows:
                    lid = bc.add_cost_line(
                        conn, existing["id"],
                        category=r["category"], description=r["description"],
                        vendor="", qty=r["qty"], unit_cost=r["unit_cost"],
                        estimated=r["price"], company=COMPANY,
                    )
                    _set_unit(conn, lid, r["unit"])
                bo.set_bid_amount(conn, existing["id"], round(total_price, 2), COMPANY)
                print(f"Repriced: {len(rows)} lines, bid now ${total_price:,.2f}")
            if "--refresh" in sys.argv:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE bid_opportunities SET scope = %s, notes = %s, "
                        "updated_at = now() WHERE id = %s AND company = %s",
                        (SCOPE.strip(), NOTES.strip(), existing["id"], COMPANY),
                    )
                conn.commit()
                print("Refreshed scope and notes on the existing card.")
            print(f"Open it: /bids/{existing['id']}")
            print(f"Package: /bids/{existing['id']}/package.pdf")
            return

        due = (date.today() + timedelta(days=14)).isoformat()
        bid_id = bo.create_bid(
            conn, COMPANY,
            name="Hallway, Runner and Bathroom Renovation",
            owner="",
            trade="Interior renovation",
            location="",
            reference=REFERENCE,
            due_date=due,
            bid_amount=None,
            status="drafting",
            notes=NOTES.strip(),
            scope=SCOPE.strip(),
        )
        print(f"\nCreated bid #{bid_id}")

        for r in rows:
            lid = bc.add_cost_line(
                conn, bid_id,
                category=r["category"],
                description=r["description"],
                vendor="",
                qty=r["qty"],
                unit_cost=r["unit_cost"],
                estimated=r["price"],
                company=COMPANY,
            )
            _set_unit(conn, lid, r["unit"])
        bo.set_bid_amount(conn, bid_id, round(total_price, 2), COMPANY)
        print(f"Wrote {len(rows)} cost lines and set the bid to ${total_price:,.2f}")
        print(f"Open the card: /bids/{bid_id}")
        print(f"Download the package: /bids/{bid_id}/package.pdf")
    finally:
        if dry_run:
            conn.rollback()
        conn.close()


if __name__ == "__main__":
    main()
