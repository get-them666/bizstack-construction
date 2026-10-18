"""Materials pricing for Buildstack Construction Co.

Three layers, in order of how much they can be trusted:

1. **A real supplier feed** (``supplier_prices``) -- actual prices from a
   supplier, written by ``sync_into_catalogue`` or typed in by the owner. This
   is the only layer that is a real price, and the only one a quote may use.

2. **EstimationPro regional costs** (``estimation_pro``) -- a public, keyless
   benchmark that resolves a ZIP to a region and returns dated, itemised costs.
   Real data, but a benchmark rather than something we were invoiced.

3. **This module's estimate layer** (``PRICE_BOOK`` plus the inline line
   ranges). Internal allowances. They are escalated by the official BLS
   construction-materials PPI from ``PRICE_BOOK_BASE_PERIOD`` so they track the
   industry, and every surface that shows them says "estimate". An index
   supplies a ratio, not a level: escalation makes these numbers current, it
   cannot make them real. See ``cost_indices``.

No fabricated price is presented as a price. The seeded brand catalogue
(``BRAND_CATALOG``) carries real brands, styles and store SKUs but no prices at
all -- a 0 means "not currently priced" and nothing quotes it.

The copilot tool ``estimate_materials(project_type, sqft, include=[], live=...)``
returns a line-item materials estimate with its ``source`` and the BLS period
behind it. Never quote a final materials price from memory -- always pull from
this service, and prefer a supplier price over an estimate.
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

__all__ = ["BusinessMaterialsService", "PRICE_BOOK", "DEFAULT_ESTIMATE_UNITS", "BRAND_CATALOG"]

# ---------------------------------------------------------------------------
# Estimate book (cents). Internal allowances struck around 2026-01; NOT supplier
# prices and NOT quotable. Every read goes through _escalated_cents(), which
# moves it forward by the official BLS construction-materials PPI, and every
# surface that shows it labels it an estimate. See bls_escalation below.
# ---------------------------------------------------------------------------
PRICE_BOOK: dict = {
    # --- Lumber / framing (MBF = thousand board feet) -------------------------
    "lumber_per_mbf_est": 98000,      # framing lumber, ~$980/MBF national index est.
    "lumber_premium_per_mbf_est": 112700, # premium/select lumber, +15% over standard
    "stud_2x4x8": 545,                # ~$5.45 ea (SPF #2)
    "stud_2x4x10": 685,
    "stud_2x6x8": 820,
    "lumber_engineered_per_mbf_est": 117600, # engineered LVL/I-joist, +20% over standard
    "drywall_sheet_1/2": 1444,        # ~$14.44/sheet 4x8
    "drywall_sheet_5/8_type_x": 1690,
    "drywall_mud_5gal": 1890,
    "paint_gallon_interior": 3850,
    "paint_gallon_exterior": 4650,
    # --- Roofing (per 100 sq ft "square") ------------------------------------
    "shingles_per_square": 9358,      # ~$93.58/sq (architectural, asphalt)
    "shingles_3tab_per_square": 7490, # ~$74.90/sq (3-tab, 25yr)
    "shingles_arch_per_square": 9358, # ~$93.58/sq (architectural, 30yr)
    "shingles_prem_per_square": 10980,# ~$109.80/sq (premium architectural, 40yr/50yr)
    "metal_roof_per_square": 32100,   # ~$321/sq (standing-seam metal)
    "roofing_felt_roll": 1850,
    "ice_water_shield_roll": 2650,
    "roof_nails_30lb": 2850,
    # --- Electrical -----------------------------------------------------------
    "copper_wire_per_lb": 484,        # ~$4.84/lb (THHN/sealtite est.)
    "romex_12_2_250ft": 7178,         # ~$71.78/roll
    "romex_14_2_250ft": 6190,
    "breaker_20a_bolt_on": 595,
    "outlet_receptacle_duplex": 240,
    "panel_200a_main": 41900,
    "service_entrance_100a": 29500,
    # --- Plumbing / PEX --------------------------------------------------------
    "pex_a_1/2_per_ft": 68,
    "pex_a_3/4_per_ft": 94,
    "pex_crimp_ring_1/2": 22,
    "copper_pipe_1/2_per_ft": 215,
    "water_heater_50gal": 61800,
    "toilet_1pc": 24500,
    "sink_stainless_33in": 16900,
    "vanity_36in": 17400,
    # --- Tile / flooring --------------------------------------------------------
    "tile_ceramic_sqft": 415,
    "tile_porcelain_sqft": 495,
    "grout_25lb": 1390,
    "thinset_50lb": 1590,
    "lvp_sqft": 359,
    "laminate_sqft": 245,
    "carpet_sqft": 320,
    # --- Countertops (per sq ft) ---------------------------------------------
    "countertop_laminate_sqft": 1090,
    "countertop_quartz_sqft": 4650,
    "countertop_granite_sqft": 4100,
    "countertop_marble_sqft": 6900,
    # --- Concrete / masonry -----------------------------------------------------
    "concrete_ready_mix_cuyd": 16400,  # ~$164/cy delivered
    "concrete_bag_80lb": 630,
    "rebar_#4_per_ft": 115,
    "masonry_block_8in": 165,
    # --- Fasteners / consumables -------------------------------------------------
    "framing_nails_30lb": 3150,
    "deck_screw_galv_10lb": 2450,
    "drywall_screw_5lb": 1150,
    "construction_adhesive_tube": 695,
    "painters_tape_roll": 455,
    "caulk_silicone_tube": 525,
}

# Units used by the default estimate builder (key -> human unit label).
DEFAULT_ESTIMATE_UNITS: dict = {
    "lumber_per_mbf_est": "MBF",
    "lumber_premium_per_mbf_est": "MBF",
    "lumber_engineered_per_mbf_est": "MBF",
    "stud_2x4x8": "ea", "stud_2x4x10": "ea", "stud_2x6x8": "ea",
    "drywall_sheet_1/2": "sheets", "drywall_sheet_5/8_type_x": "sheets",
    "drywall_mud_5gal": "buckets",
    "paint_gallon_interior": "gal", "paint_gallon_exterior": "gal",
    "shingles_per_square": "sq", "shingles_3tab_per_square": "sq",
    "shingles_arch_per_square": "sq", "shingles_prem_per_square": "sq",
    "metal_roof_per_square": "sq",
    "roofing_felt_roll": "rolls",
    "ice_water_shield_roll": "rolls", "roof_nails_30lb": "boxes",
    "copper_wire_per_lb": "lb", "romex_12_2_250ft": "roll",
    "romex_14_2_250ft": "roll", "breaker_20a_bolt_on": "ea",
    "outlet_receptacle_duplex": "ea", "panel_200a_main": "ea",
    "service_entrance_100a": "ea",
    "pex_a_1/2_per_ft": "ft", "pex_a_3/4_per_ft": "ft",
    "pex_crimp_ring_1/2": "ea", "copper_pipe_1/2_per_ft": "ft",
    "water_heater_50gal": "ea", "toilet_1pc": "ea",
    "sink_stainless_33in": "ea", "vanity_36in": "ea",
    "tile_ceramic_sqft": "sqft", "tile_porcelain_sqft": "sqft",
    "grout_25lb": "bags", "thinset_50lb": "bags",
    "lvp_sqft": "sqft", "laminate_sqft": "sqft", "carpet_sqft": "sqft",
    "countertop_laminate_sqft": "sqft", "countertop_quartz_sqft": "sqft",
    "countertop_granite_sqft": "sqft", "countertop_marble_sqft": "sqft",
    "concrete_ready_mix_cuyd": "cuyd", "concrete_bag_80lb": "bags",
    "rebar_#4_per_ft": "ft", "masonry_block_8in": "pcs",
    "framing_nails_30lb": "boxes", "deck_screw_galv_10lb": "boxes",
    "drywall_screw_5lb": "boxes", "construction_adhesive_tube": "tubes",
    "painters_tape_roll": "rolls", "caulk_silicone_tube": "tubes",
}


# ---------------------------------------------------------------------------
# Brand catalog. Real brands, styles and store item/SKU numbers so the
# assistant can name a specific product when it quotes.
#
# NO PRICE IS SEEDED HERE. These rows used to carry hand-typed figures whose own
# notes read "2026 retail ballpark" and "2026 typical retail range" -- invented
# numbers indistinguishable, on screen, from a price anyone had actually paid.
# They are gone. price_cents stays 0, which `price_list` and the public
# catalogue already treat as "not currently priced"; nothing quotes a 0.
#
# A price only gets written to this table from a real source: a supplier feed
# (see supplier_prices.py) or an explicit owner entry. BLS PPI can escalate such
# a price once it exists -- see cost_indices.py -- but an index cannot invent
# the level, which is exactly why the levels were removed rather than adjusted.
#
# Fields: store, brand, category, name, model, sku, style, color, unit.
# ---------------------------------------------------------------------------
BRAND_CATALOG: list = [
    # --- Kitchen / bath cabinets (Home Depot, Hampton Bay) ------------------
    {"store": "Home Depot", "brand": "Hampton Bay", "category": "cabinets",
     "name": "Designer Series Elgin 36x24x34.5 Assembled Corner Base Cabinet",
     "model": "BEZ36-ELWH", "sku": "305842616", "style": "Raised Panel",
     "color": "White", "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "Hampton Bay", "category": "cabinets",
     "name": "Shaker 36x24x34.5 Ready-to-Assemble Corner Sink Base Cabinet",
     "model": "KCSB36-SSW", "sku": "", "style": "Shaker", "color": "Satin White",
     "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "Hampton Bay", "category": "cabinets",
     "name": "Shaker 36x24x34.5 Ready-to-Assemble Corner Sink Base Cabinet",
     "model": "KCSB36-SDV", "sku": "", "style": "Shaker", "color": "Dove Gray",
     "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    # --- Roofing (GAF, Home Depot + Lowe's) ---------------------------------
    {"store": "Home Depot", "brand": "GAF", "category": "roofing",
     "name": "Royal Sovereign 3-Tab Roofing Shingles (33.33 sq ft/bundle)",
     "model": "0201180", "sku": "", "style": "3-Tab", "color": "Charcoal",
     "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "GAF", "category": "roofing",
     "name": "Royal Sovereign 3-Tab Roofing Shingles (33.33 sq ft/bundle)",
     "model": "0202880", "sku": "", "style": "3-Tab", "color": "Weathered Gray",
     "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "GAF", "category": "roofing",
     "name": "Timberline HDZ Architectural Laminated Shingles (33.33 sq ft/bundle)",
     "model": "0489180", "sku": "", "style": "Architectural", "color": "Charcoal",
     "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "GAF", "category": "roofing",
     "name": "Timberline HDZ Architectural Laminated Shingles (33.33 sq ft/bundle)",
     "model": "0489258", "sku": "", "style": "Architectural", "color": "Driftwood",
     "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "GAF", "category": "roofing",
     "name": "Timberline UHDZ Ultra-Manatee Laminated Shingles (pro, special order)",
     "model": "0582900", "sku": "1015001911", "style": "Architectural",
     "color": "Weathered Wood", "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Lowe's", "brand": "GAF", "category": "roofing",
     "name": "Timberline HDZ Architectural Roof Shingles (33.33 sq ft/bundle)",
     "model": "0489180", "sku": "1439810", "style": "Architectural",
     "color": "Charcoal", "unit": "bundle",
     "notes": "No price: awaiting a real supplier feed."},
    # --- Flooring (LVP, Home Depot) ------------------------------------------
    {"store": "Home Depot", "brand": "TrafficMaster", "category": "flooring",
     "name": "Bighorn Trail Oak Rigid-Lock Waterproof LVP (7.2x48 in)",
     "model": "TM2151", "sku": "", "style": "LVP", "color": "Trail Oak",
     "unit": "sqft",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "A&A Surfaces", "category": "flooring",
     "name": "Acorn Hill Luxury Vinyl Plank Flooring",
     "model": "HD-LVR6550-0012", "sku": "", "style": "LVP", "color": "Acorn Hill",
     "unit": "sqft",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "Shaw", "category": "flooring",
     "name": "Camden Mindful Luxury Vinyl Plank Flooring",
     "model": "HD94500295", "sku": "", "style": "LVP", "color": "Camden Mindful",
     "unit": "sqft",
     "notes": "No price: awaiting a real supplier feed."},
    # --- Fixtures (Home Depot) ------------------------------------------------
    {"store": "Home Depot", "brand": "MOEN", "category": "fixtures",
     "name": "Genta 4in Centerset Double-Handle Bathroom Faucet w/ Drain",
     "model": "84764BL", "sku": "1012308602", "style": "Centerset",
     "color": "Matte Black", "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "MOEN", "category": "fixtures",
     "name": "Chateau Single-Handle Fixed Kitchen Faucet w/ Side Sprayer",
     "model": "7430", "sku": "", "style": "Single-Handle", "color": "Chrome",
     "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot", "brand": "Delta", "category": "fixtures",
     "name": "Foundations Single-Handle 1-Spray Tub & Shower Faucet (valve incl.)",
     "model": "B114900C", "sku": "", "style": "Tub/Shower", "color": "Chrome",
     "unit": "each",
     "notes": "No price: awaiting a real supplier feed."},
    # --- Countertops (typical retail range; verified on-site) -----------------
    {"store": "Home Depot / Lowe's", "brand": "Engineered Quartz", "category": "countertops",
     "name": "Engineered quartz countertop (slab, standard grade)",
     "model": "", "sku": "", "style": "Quartz", "color": "any",
     "unit": "sqft",
     "notes": "No price: awaiting a real supplier feed."},
    {"store": "Home Depot / Lowe's", "brand": "Granite", "category": "countertops",
     "name": "Granite countertop (slab, standard grade)",
     "model": "", "sku": "", "style": "Granite", "color": "any",
     "unit": "sqft",
     "notes": "No price: awaiting a real supplier feed."},
]


# Catalog rows loaded from the shared `materials_catalog` table (owner-editable
# superset of BRAND_CATALOG). Seeded + refreshed from the app startup hook.
_catalog_rows: list = []


def _cents(x) -> int:
    try:
        return max(0, int(round(float(x or 0))))
    except (TypeError, ValueError):
        return 0


def include_bool(include: list, key: str) -> bool:
    """True when `include` contains key (casing-insensitive), else False."""
    return any(str(k).strip().lower() == key for k in (include or []))


# ---------------------------------------------------------------------------
# Official escalation for the internal estimate layer
# ---------------------------------------------------------------------------
# The levels in PRICE_BOOK and in the inline line ranges below were struck as
# "2026-era ballparks". They are internal judgement, not quotes, and they are
# labelled as estimates wherever they surface. What BLS can honestly fix is the
# one thing that is knowable without a supplier: how far those levels have
# drifted since they were set. So the whole estimate layer is multiplied by the
# official construction-materials PPI between PRICE_BOOK_BASE_PERIOD and the
# latest published month.
#
# This makes the numbers current. It does not make them real: an index supplies
# a ratio, never a level. See cost_indices.verify_anchor, which returns
# "not_checkable_by_an_index" for exactly that reason.
PRICE_BOOK_BASE_PERIOD = os.getenv("PRICE_BOOK_BASE_PERIOD", "2026-01")

_escalation_cache: dict = {"at": 0.0, "payload": None}
_ESCALATION_TTL = 6 * 3600


def bls_escalation() -> dict:
    """Current/then multiplier for construction materials, cached.

    Degrades to 1.0 when BLS cannot be reached, and says so via ``escalated``
    so callers can label the estimate instead of quietly presenting stale
    numbers as current.
    """
    now = time.time()
    if _escalation_cache["payload"] and now - _escalation_cache["at"] < _ESCALATION_TTL:
        return _escalation_cache["payload"]
    try:
        import cost_indices as ci
        result = ci.escalate(100, ci.DEFAULT_MATERIALS, PRICE_BOOK_BASE_PERIOD)
        payload = {
            "factor": float(result.get("factor") or 1.0) if result.get("escalated") else 1.0,
            "escalated": bool(result.get("escalated")),
            "reason": result.get("reason"),
            "bls_series": result.get("bls_series"),
            "bls_period": result.get("bls_period"),
            "bls_index": result.get("bls_index"),
            "base_period": PRICE_BOOK_BASE_PERIOD,
        }
    except Exception as e:
        payload = {"factor": 1.0, "escalated": False, "reason": str(e)[:200],
                   "bls_series": None, "bls_period": None, "bls_index": None,
                   "base_period": PRICE_BOOK_BASE_PERIOD}
    _escalation_cache["at"] = now
    _escalation_cache["payload"] = payload
    return payload


def _escalated_cents(cents) -> int:
    """Move one estimate value forward with the official index."""
    value = _cents(cents)
    if not value:
        return value
    factor = bls_escalation().get("factor") or 1.0
    return int(round(value * factor)) if factor != 1.0 else value


# --- Brand catalog: DB persistence + search --------------------------------
def _cat_dollar(row: dict) -> float:
    return round(_cents(row.get("price_cents")) / 100, 2)


def catalog() -> list:
    """Active catalog: owner rows from DB when loaded, else the seeded book."""
    return _catalog_rows if _catalog_rows else BRAND_CATALOG


NO_PRICE_NOTE = "No price: awaiting a real supplier feed."

# The exact phrases the hand-typed seed rows carried. Matching on these rather
# than on "price_cents > 0" is deliberate: it clears the invented numbers and
# leaves every row the owner entered by hand alone.
_FABRICATED_MARKERS = (
    "%retail ballpark%",
    "%typical retail range%",
    "%2026 retail%",
    "%2026 typical retail%",
    "%2026-era ballpark%",
)


def retire_fabricated_prices(conn) -> int:
    """Zero out seeded prices that were never a real quote.

    ``seed_materials_catalog`` only seeds an empty table, so removing the prices
    from ``BRAND_CATALOG`` does not touch the rows a live database already
    holds. Without this those 17 invented prices keep rendering exactly as
    before, which would make the removal cosmetic.

    Idempotent: a second run matches nothing, because the notes it writes do not
    contain any of the markers.
    """
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE materials_catalog
                SET price_cents = 0, price_high_cents = NULL,
                    notes = %s, updated_at = NOW()
                WHERE price_cents <> 0
                  AND ({" OR ".join(["notes ILIKE %s"] * len(_FABRICATED_MARKERS))})""",
            (NO_PRICE_NOTE, *_FABRICATED_MARKERS))
        cleared = cur.rowcount or 0
    if cleared:
        conn.commit()
        print(f"🧹 materials: cleared {cleared} fabricated seeded price(s)", flush=True)
    return cleared


def seed_materials_catalog(conn) -> int:
    """Create `materials_catalog` and seed BRAND_CATALOG when the table is empty."""
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS materials_catalog (
            id SERIAL PRIMARY KEY,
            store VARCHAR(120) NOT NULL DEFAULT '',
            brand VARCHAR(120) NOT NULL DEFAULT '',
            category VARCHAR(80) NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            model VARCHAR(120) NOT NULL DEFAULT '',
            sku VARCHAR(120) NOT NULL DEFAULT '',
            style VARCHAR(80) NOT NULL DEFAULT '',
            color VARCHAR(80) NOT NULL DEFAULT '',
            unit VARCHAR(40) NOT NULL DEFAULT 'each',
            price_cents INTEGER NOT NULL DEFAULT 0,
            price_high_cents INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_catalog_cat ON materials_catalog (category, brand, enabled);")
        retire_fabricated_prices(conn)
        cur.execute("SELECT COUNT(*) AS c FROM materials_catalog;")
        row = cur.fetchone()
        count = row["c"] if isinstance(row, dict) else (row[0] if row else 0)
        if count == 0:
            for r in BRAND_CATALOG:
                cur.execute(
                    """INSERT INTO materials_catalog
                       (store, brand, category, name, model, sku, style, color, unit,
                        price_cents, price_high_cents, notes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
                    (
                        r.get("store", ""), r.get("brand", ""), r.get("category", ""),
                        r.get("name", ""), r.get("model", ""), r.get("sku", ""),
                        r.get("style", ""), r.get("color", ""), r.get("unit", "each"),
                        _cents(r.get("price_cents")), r.get("price_high_cents"),
                        r.get("notes", ""),
                    ),
                )
        conn.commit()
        cur.execute("SELECT COUNT(*) AS c FROM materials_catalog;")
        row = cur.fetchone()
        return row["c"] if isinstance(row, dict) else (row[0] if row else 0)


def refresh_catalog(conn) -> int:
    """Load the owner-editable catalog rows into the active catalog cache."""
    global _catalog_rows
    _catalog_rows = [
        {
            "store": r["store"], "brand": r["brand"], "category": r["category"],
            "name": r["name"], "model": r["model"], "sku": r["sku"],
            "style": r["style"], "color": r["color"], "unit": r["unit"],
            "price_cents": int(r["price_cents"] or 0),
            "price_high_cents": int(r["price_high_cents"]) if r.get("price_high_cents") else None,
            "notes": r["notes"],
        }
        for r in _catalog_rows_from_db(conn) if r.get("enabled", True)
    ]
    return len(_catalog_rows)


def _catalog_rows_from_db(conn) -> list:
    if conn is None:
        return []
    try:
        seed_materials_catalog(conn)
    except Exception as e:
        print(f"⚠️ materials_catalog ensure/seed failed: {e}")
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM materials_catalog WHERE enabled = TRUE "
            "ORDER BY category, brand, model, id;"
        )
        rows = cur.fetchall()
        if not rows:
            return []
        first = rows[0]
        if isinstance(first, dict):
            return rows
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


def search_materials(
    category: str = "",
    brand: str = "",
    query: str = "",
    store: str = "",
    limit: int = 8,
) -> dict:
    """Search the brand catalog (owner-editable) by category/brand/store/text.

    Returns matches with dollars fields, ready to speak. Never fabricate a brand
    or SKU — only what's in the catalog.
    """
    q = (query or "").strip().lower()
    want_cat = [c.lower() for c in _split(category)]
    want_brand = [b.lower() for b in _split(brand)]
    want_store = [s.lower() for s in _split(store)]
    hits = []
    for row in catalog():
        hay = " ".join(
            str(row.get(k) or "") for k in ("category", "brand", "name", "model", "sku", "style", "color", "store", "notes")
        ).lower()
        if want_cat and not any(c in hay for c in want_cat):
            continue
        if want_brand and not any(b in hay for b in want_brand):
            continue
        if want_store and not any(s in hay for s in want_store):
            continue
        if q and q not in hay:
            continue
        out = dict(row)
        out["price_dollars"] = _cat_dollar(row)
        out["price_high_dollars"] = (
            round(_cents(row.get("price_high_cents")) / 100, 2)
            if row.get("price_high_cents") else None
        )
        hits.append(out)
    hits.sort(key=lambda r: _cents(r.get("price_cents")) or 0)
    if limit and isinstance(limit, int) and limit > 0:
        hits = hits[:limit]
    return {"ok": True, "count": len(hits), "items": hits,
            "source": "materials-catalog"}


def _split(text: str) -> list:
    return [p for p in str(text or "").replace(",", " ").split() if p]


class BusinessMaterialsService:
    """Price book + optional live-API seam for material estimates."""

    def __init__(self, api_url: str = "", api_key: str = ""):
        self._api_url = api_url or os.getenv("MATERIALS_API_URL", "")
        self._api_key = api_key or os.getenv("MATERIALS_API_KEY", "")
        self._cache: dict = {}
        self._cache_until: Optional[float] = None

    # --- Live-API seam --------------------------------------------------------
    def api_configured(self) -> bool:
        return bool(self._api_url and self._api_key)

    def _cache_seconds(self) -> int:
        try:
            return max(int(os.getenv("MATERIALS_CACHE_MINUTES", "720")) * 60, 60)
        except (TypeError, ValueError):
            return 43200

    def get_price(self, sku: str) -> Optional[int]:
        """Live price (cents) for a sku when API configured + cached, else book.

        A book value is an internal allowance, so it is escalated by the official
        PPI before it is used. See ``bls_escalation``.
        """
        if self.api_configured():
            now = time.time()
            if now > (self._cache_until or 0):
                self._refresh_cache()
            if sku in self._cache:
                return self._cache[sku]
        book = PRICE_BOOK.get(sku)
        return _escalated_cents(book) if book is not None else None

    def _refresh_cache(self) -> None:
        """Pull live prices from the configured materials API and cache them."""
        if not self.api_configured():
            return
        try:
            import urllib.request as ur

            url = f"{self._api_url.rstrip('/')}/prices?{urlencode({'key': self._api_key})}"
            with ur.urlopen(url, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            prices = data.get("prices", data) if isinstance(data, dict) else data
            if isinstance(prices, dict):
                for sku, cents in prices.items():
                    self._cache[sku] = _cents(cents)
                self._cache_until = time.time() + self._cache_seconds()
        except Exception as e:  # degrade gracefully to price book
            print(f"⚠️ materials API unavailable ({e}); using price book.")

    # --- Brand catalog (owner-editable; seeded from BRAND_CATALOG) ----------
    def refresh_from_db(self, conn) -> int:
        """Reload the active catalog from the shared materials_catalog table."""
        return refresh_catalog(conn)

    def search_materials(self, category="", brand="", query="", store="", limit=8) -> dict:
        """Search the owner-editable brand catalog (brands, styles, SKUs)."""
        return search_materials(category=category, brand=brand, query=query,
                                store=store, limit=limit)

    # --- Estimate builder ------------------------------------------------------
    def estimate_materials(
        self,
        project_type: str = "",
        sqft: float = 0,
        include: Optional[list] = None,
        live: bool = False,
    ) -> dict:
        """Build a line-item materials estimate for a project type."""
        include = include or []
        t = (project_type or "handyman").strip().lower()
        sqft = max(float(sqft or 0), 0)
        cents_total_low, cents_total_high = 0, 0
        lines: list = []
        live_ok = live and self.api_configured()

        def add(name, sku, qty, low_cents, high_cents, unit):
            nonlocal cents_total_low, cents_total_high
            qty = max(float(qty or 0), 0)
            # Every line's range is an internal ballpark, escalated together by
            # the official PPI. Applied here because this is the only place a
            # line is priced, so nothing can bypass it.
            low_c = _escalated_cents(low_cents) * qty
            high_c = _escalated_cents(high_cents) * qty
            cents_total_low += low_c
            cents_total_high += high_c
            sku_unit = unit or DEFAULT_ESTIMATE_UNITS.get(sku, "unit")
            lines.append(
                {
                    "name": name,
                    "sku": sku,
                    "qty": round(qty, 2),
                    "unit": sku_unit,
                    "low_cents": int(low_c),
                    "high_cents": int(high_c),
                }
            )

        # Typical per-type line items; quantities scale with square footage.
        if t in ("whole-home", "renovation", "remodel", "whole", "addition", "add"):
            add("Framing lumber (MBF est.)", "lumber_per_mbf_est", max(0.05, sqft * 0.6 / 1000), 180000, 240000, "MBF")
            add("Plywood sheathing", "drywall_sheet_1/2", max(4, sqft * 0.62), 1300, 1550, "sheets")
            add("Pressure-treated sill", "stud_2x4x10", max(8, sqft * 0.02), 685, 820, "ea")
            add("Fasteners", "framing_nails_30lb", max(1, sqft * 0.004), 2950, 3150, "boxes")
            if include_bool(include, "electric") or include_bool(include, "electrical"):
                add("Romex 12/2 (NM)", "romex_12_2_250ft", max(1, sqft * 0.004), 6500, 7200, "roll")
                add("Outlets/receptacles", "outlet_receptacle_duplex", max(6, sqft * 0.014), 240, 260, "ea")
            if include_bool(include, "plumbing"):
                add("PEX-A 1/2\" supply", "pex_a_1/2_per_ft", max(30, sqft * 0.18), 62, 72, "ft")
        elif t in ("kitchen",):
            add("Cabinets (est. allowance)", "vanity_36in", 6, 17400, 21000, "ea")
            add("Countertop laminate", "tile_porcelain_sqft", max(10, sqft * 0.2), 495, 540, "sqft")
            add("Tile backsplash", "tile_ceramic_sqft", max(18, sqft * 0.4), 375, 425, "sqft")
            add("Grout + thinset", "grout_25lb", 0, 0, 0, "bags")
            add("Sink + faucet allowance", "sink_stainless_33in", 1, 16000, 19000, "ea")
            add("PEX supply (ft)", "pex_a_1/2_per_ft", 30, 62, 72, "ft")
        elif t in ("bath", "bathroom"):
            add("Tile floor/wall", "tile_ceramic_sqft", max(40, sqft * 1.4), 375, 425, "sqft")
            add("Vanity", "vanity_36in", 1, 16000, 18500, "ea")
            add("Toilet", "toilet_1pc", 1, 22800, 26500, "ea")
            add("PEX supply", "pex_a_3/4_per_ft", 12, 88, 100, "ft")
            add("Grout", "grout_25lb", 1, 1300, 1500, "bags")
        elif t in ("roofing", "roof", "shingles", "siding"):
            add("Shingles", "shingles_per_square", max(2, sqft / 100), 8600, 9800, "sq")
            add("Roofing felt", "roofing_felt_roll", max(1, sqft * 0.008), 1750, 1950, "rolls")
            add("Ice & water shield", "ice_water_shield_roll", max(1, sqft * 0.005), 2400, 2750, "rolls")
            add("Roof nails", "roof_nails_30lb", max(1, sqft * 0.003), 2700, 3000, "boxes")
        elif t in ("drywall", "drywall-paint", "paint", "interior"):
            add("1/2\" drywall", "drywall_sheet_1/2", max(3, sqft * 0.425), 1300, 1525, "sheets")
            add("5/8\" type-X (garage/ceilings)", "drywall_sheet_5/8_type_x", max(1, sqft * 0.12), 1550, 1750, "sheets")
            add("Joint compound", "drywall_mud_5gal", max(1, sqft * 0.012), 1750, 2050, "buckets")
            add("Interior paint", "paint_gallon_interior", max(2, sqft * 0.008), 3500, 4200, "gal")
            add("Drywall screws", "drywall_screw_5lb", max(1, sqft * 0.002), 525, 700, "boxes")
        elif t in ("deck", "fence", "deck-fence", "porch"):
            add("Decking boards", "stud_2x6x8", max(4, sqft / 17), 820, 960, "ea")
            add("Posts", "stud_2x4x10", max(2, sqft / 60), 685, 780, "ea")
            add("Galv fasteners", "deck_screw_galv_10lb", max(1, sqft / 400), 2200, 2500, "boxes")
            add("Concrete piers", "concrete_bag_80lb", max(2, sqft / 90), 590, 640, "bags")
        else:
            add("Bulk materials (est. line)", "lumber_per_mbf_est", max(0.04, sqft * 0.5 / 1000), 176000, 235000, "MBF")
            add("Fasteners", "framing_nails_30lb", 1, 2850, 3200, "boxes")

        # Include-only explicit skus (owner asked about these).
        for sku in include:
            sku = str(sku).strip()
            if sku in PRICE_BOOK and sku not in [ln["sku"] for ln in lines]:
                cents = _escalated_cents(self.get_price(sku) or PRICE_BOOK[sku])
                add(sku.replace("_", " "), sku, 1, int(cents * 0.95), int(cents * 1.15),
                    DEFAULT_ESTIMATE_UNITS.get(sku, "unit"))

        dollars = lambda c: round(c / 100, 2)  # noqa: E731
        esc = bls_escalation()
        if live_ok:
            source = "live-api"
            note = ("Live material prices via API — final price still confirmed "
                    "on-site.")
        elif esc["escalated"]:
            source = f"estimate+{esc['bls_series']}@{esc['bls_period']}"
            note = (
                f"Estimate. Our internal allowance from {esc['base_period']}, "
                f"escalated to {esc['bls_period']} by the official BLS "
                f"{esc['bls_series']} construction-materials index. "
                f"Levels are judgement, not a supplier quote — every price is "
                f"confirmed against a real receipt before it goes on a quote."
            )
        else:
            source = "estimate-stale"
            note = (
                f"Estimate, and NOT index-escalated: {esc.get('reason') or 'no BLS data'}. "
                f"Allowances are dated from {esc['base_period']} and materials "
                f"prices have moved since. Treat as a rough range and confirm "
                f"against a real receipt."
            )
        return {
            "ok": True,
            "project_type": t,
            "sqft": sqft,
            "source": source,
            "lines": lines,
            "total_low_dollars": dollars(cents_total_low),
            "total_high_dollars": dollars(cents_total_high),
            "pricing": {
                "escalated": esc["escalated"],
                "bls_series": esc["bls_series"],
                "bls_period": esc["bls_period"],
                "bls_index": esc["bls_index"],
                "base_period": esc["base_period"],
                "absolute_source": "not a supplier price; an internal allowance",
            },
            "note": note,
        }


# --------------------------------------------------------------------------
# Owner-facing catalog editing
# --------------------------------------------------------------------------
# Why this exists: the supplier feeds are the destination, not the starting
# point. ABC Supply needs a credit account and an approved enrollment; Lowe's
# needs a partner agreement; Richards, Kempsville and Builders FirstSource have
# no feed at all. Until any of those land, the only way a quote is accurate is
# if a human types the real numbers in -- and that has to be fast enough that
# they actually do it. So: one row at a time from a receipt, or a paste of
# many rows at once, into the same table the feeds write to.
#
# Prices are entered in DOLLARS because that is what is printed on the invoice
# and what anyone reads off Lowe's' shelf or a Kempsville ticket. They are
# stored in cents, which is what quoting multiplies.

_CATALOG_FIELDS = ("store", "brand", "category", "name", "model", "sku",
                   "unit", "notes")


def _to_cents(dollars) -> int | None:
    """Dollars (string or number) -> integer cents, or None if unusable."""
    if dollars in (None, ""):
        return None
    s = str(dollars).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("(") and s.endswith(")"):      # accounting negative
        s = "-" + s[1:-1]
    try:
        return int(round(float(s) * 100))
    except (TypeError, ValueError):
        return None


def catalog_rows(conn, *, limit: int = 500, search: str = "",
                 store: str = "", category: str = "") -> list[dict]:
    """Rows for the admin table, newest-updated first."""
    if conn is None:
        return []
    where, params = ["enabled"], []
    if search:
        where.append("(name ILIKE %s OR brand ILIKE %s OR sku ILIKE %s)")
        params += [f"%{search}%"] * 3
    if store:
        where.append("store = %s")
        params.append(store)
    if category:
        where.append("category = %s")
        params.append(category)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, store, brand, category, name, model, sku, unit, "
                "price_cents, notes, updated_at FROM materials_catalog "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY updated_at DESC LIMIT %s;",
                params + [int(limit)],
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[materials] catalog_rows failed: {exc}", flush=True)
        return []


def catalog_facets(conn) -> dict:
    """Distinct stores/categories, for the filter dropdowns."""
    out = {"stores": [], "categories": []}
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT store FROM materials_catalog "
                        "WHERE store <> '' ORDER BY store;")
            out["stores"] = [r["store"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT category FROM materials_catalog "
                        "WHERE category <> '' ORDER BY category;")
            out["categories"] = [r["category"] for r in cur.fetchall()]
    except Exception:
        pass
    return out


def upsert_catalog_item(conn, *, name: str, price_dollars, store: str = "",
                        brand: str = "", category: str = "", model: str = "",
                        sku: str = "", unit: str = "each", notes: str = "",
                        item_id: int | None = None) -> dict:
    """Add or update one catalog row.

    A blank price is rejected rather than stored as 0. Zero is a real price in
    this system -- a giveaway line, a free item -- and defaulting a forgotten
    price to it would put a $0 on a customer's quote.
    """
    if conn is None:
        return {"ok": False, "error": "no database"}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    cents = _to_cents(price_dollars)
    if cents is None:
        return {"ok": False, "error": "price is required and must be a number"}
    if cents < 0:
        return {"ok": False, "error": "price cannot be negative"}
    try:
        with conn.cursor() as cur:
            if item_id:
                cur.execute(
                    "UPDATE materials_catalog SET store=%s, brand=%s, category=%s, name=%s, "
                    "model=%s, sku=%s, unit=%s, price_cents=%s, notes=%s, updated_at=NOW() "
                    "WHERE id=%s RETURNING id;",
                    (store or "", brand or "", category or "", name, model or "",
                     sku or "", unit or "each", cents, notes or "", int(item_id)),
                )
                if not cur.fetchone():
                    item_id = None
            if not item_id:
                cur.execute(
                    "INSERT INTO materials_catalog (store, brand, category, name, model, sku, "
                    "style, color, unit, price_cents, price_high_cents, notes, enabled) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,NULL,%s,TRUE) RETURNING id;",
                    (store or "", brand or "", category or "", name, model or "",
                     sku or "", unit or "each", cents, notes or ""),
                )
                item_id = (cur.fetchone() or {}).get("id")
        conn.commit()
        return {"ok": True, "id": item_id, "price_cents": cents}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)}


def bulk_import_catalog(conn, text: str, *, default_store: str = "") -> dict:
    """Paste many rows at once.

    Accepts CSV or TSV. Header row optional; recognised columns are
    name/item, price, store/supplier, brand, category, sku, model, unit, notes.
    Blank lines and lines starting with # are skipped.

    Whole-file parse before any write, so one bad line cannot leave the catalog
    half-imported -- the whole batch is rejected with the offending row named.
    """
    result = {"imported": 0, "skipped": 0, "errors": [], "total": 0}
    if conn is None:
        result["errors"].append("no database")
        return result
    if not (text or "").strip():
        result["errors"].append("nothing to import")
        return result

    rows, header = [], None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        delim = "\t" if "\t" in line else ","
        cells = [c.strip().strip('"') for c in line.split(delim)]
        low = [c.lower().strip() for c in cells]
        if header is None and any(c in ("name", "item", "description") for c in low):
            header = {name: i for i, name in enumerate(low)}
            continue
        rows.append((lineno, cells))

    if not rows:
        result["errors"].append("no data rows found")
        return result

    # With no header there is nothing to look a name up by, so fall back to the
    # documented column order. Pasting straight off a receipt usually has no
    # header row, and "header row optional" has to actually mean optional.
    POSITIONAL = ["name", "price", "store", "brand", "category",
                  "sku", "model", "unit", "notes"]

    def col(cells, *names):
        if header:
            for n in names:
                if n in header and header[n] < len(cells):
                    return cells[header[n]]
            return ""
        for n in names:
            if n in POSITIONAL:
                i = POSITIONAL.index(n)
                return cells[i] if i < len(cells) else ""
        return ""

    parsed = []
    for lineno, cells in rows:
        name = col(cells, "name", "item", "description")
        price = col(cells, "price", "cost", "amount", "unit_price")
        if not name or price == "":
            result["skipped"] += 1
            continue
        cents = _to_cents(price)
        if cents is None or cents < 0:
            result["errors"].append(f"line {lineno}: bad price {price!r}")
            continue
        parsed.append({
            "name": name, "price_cents": cents,
            "store": col(cells, "store", "supplier") or default_store or "",
            "brand": col(cells, "brand"), "category": col(cells, "category", "type"),
            "sku": col(cells, "sku", "item_number", "upc"), "model": col(cells, "model"),
            "unit": col(cells, "unit") or "each", "notes": col(cells, "notes"),
        })

    if result["errors"]:
        return result                       # reject the whole batch, write nothing
    result["total"] = len(parsed)
    try:
        with conn.cursor() as cur:
            for p in parsed:
                cur.execute(
                    "INSERT INTO materials_catalog (store, brand, category, name, model, "
                    "sku, style, color, unit, price_cents, price_high_cents, notes, enabled) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,NULL,%s,TRUE);",
                    (p["store"], p["brand"], p["category"], p["name"], p["model"],
                     p["sku"], p["unit"], p["price_cents"], p["notes"]),
                )
                result["imported"] += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        result["imported"] = 0
    return result


def delete_catalog_item(conn, item_id: int) -> dict:
    """Disable rather than delete.

    A price that turned out to be wrong should not erase the fact that the item
    was quoted, and `enabled` is what every read path already filters on.
    """
    if conn is None:
        return {"ok": False, "error": "no database"}
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE materials_catalog SET enabled = FALSE, updated_at = NOW() "
                        "WHERE id = %s RETURNING id;", (int(item_id),))
            ok = cur.fetchone() is not None
        conn.commit()
        return {"ok": ok}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)}
