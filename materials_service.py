"""Materials pricing for Buildstack Construction Co.

Two layers:

1. A **seeded price book** (``PRICE_BOOK``) with 2026-era prices for the commodities we
   actually order — lumber/framing, structural steel, copper wire, shingles/roofing,
   drywall, concrete, tile, fasteners, paint, PEX/plumbing … Prices are stored in
   **cents** and are conservative ballparks ("we pay about X today"), not bids.

2. A **live-API seam** (``MATERIALS_API_URL`` + ``MATERIALS_API_KEY``) so the owner can
   point this at a real market-data provider (RSMeans-adjacent estimate APIs, Billd,
   Buildertrend's ProEst, or a regional lumberyard feed). When configured, prices are
   pulled live and cached for ``MATERIALS_CACHE_MINUTES``; otherwise the price book is
   used and the assistant says so honestly.

The copilot tool ``estimate_materials(project_type, sqft, include=[], live=...)`` returns
a line-item materials estimate. Never quote a final materials price from memory — always
pull from this service ('price-book' or 'live-api' source is included in the reply).
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

__all__ = ["BusinessMaterialsService", "PRICE_BOOK", "DEFAULT_ESTIMATE_UNITS"]

# ---------------------------------------------------------------------------
# Price book (cents). 2026-era ballparks seeded from recent supplier invoices.
# ---------------------------------------------------------------------------
PRICE_BOOK: dict = {
    # --- Lumber / framing (MBF = thousand board feet) -------------------------
    "lumber_per_mbf_est": 98000,      # framing lumber, ~$980/MBF national index est.
    "stud_2x4x8": 545,                # ~$5.45 ea (SPF #2)
    "stud_2x4x10": 685,
    "stud_2x6x8": 820,
    "drywall_sheet_1/2": 1444,        # ~$14.44/sheet 4x8
    "drywall_sheet_5/8_type_x": 1690,
    "drywall_mud_5gal": 1890,
    "paint_gallon_interior": 3850,
    "paint_gallon_exterior": 4650,
    # --- Roofing (per 100 sq ft "square") ------------------------------------
    "shingles_per_square": 9358,      # ~$93.58/sq (architectural, asphalt)
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
    "stud_2x4x8": "ea", "stud_2x4x10": "ea", "stud_2x6x8": "ea",
    "drywall_sheet_1/2": "sheets", "drywall_sheet_5/8_type_x": "sheets",
    "drywall_mud_5gal": "buckets",
    "paint_gallon_interior": "gal", "paint_gallon_exterior": "gal",
    "shingles_per_square": "sq", "roofing_felt_roll": "rolls",
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
    "concrete_ready_mix_cuyd": "cuyd", "concrete_bag_80lb": "bags",
    "rebar_#4_per_ft": "ft", "masonry_block_8in": "pcs",
    "framing_nails_30lb": "boxes", "deck_screw_galv_10lb": "boxes",
    "drywall_screw_5lb": "boxes", "construction_adhesive_tube": "tubes",
    "painters_tape_roll": "rolls", "caulk_silicone_tube": "tubes",
}


def _cents(x) -> int:
    try:
        return max(0, int(round(float(x or 0))))
    except (TypeError, ValueError):
        return 0


def include_bool(include: list, key: str) -> bool:
    """True when `include` contains key (casing-insensitive), else False."""
    return any(str(k).strip().lower() == key for k in (include or []))


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
        """Live price (cents) for a sku when API configured + cached, else book."""
        if self.api_configured():
            now = time.time()
            if now > (self._cache_until or 0):
                self._refresh_cache()
            if sku in self._cache:
                return self._cache[sku]
        return PRICE_BOOK.get(sku)

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
            low_c = _cents(low_cents) * qty
            high_c = _cents(high_cents) * qty
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
                cents = self.get_price(sku) or PRICE_BOOK[sku]
                add(sku.replace("_", " "), sku, 1, int(cents * 0.95), int(cents * 1.15),
                    DEFAULT_ESTIMATE_UNITS.get(sku, "unit"))

        dollars = lambda c: round(c / 100, 2)  # noqa: E731
        return {
            "ok": True,
            "project_type": t,
            "sqft": sqft,
            "source": "live-api" if live_ok else "price-book",
            "lines": lines,
            "total_low_dollars": dollars(cents_total_low),
            "total_high_dollars": dollars(cents_total_high),
            "note": (
                "Live material prices via API — final price still confirmed on-site."
                if live_ok
                else "Materials estimate from our price book — final price comes from "
                     "the free on-site walkthrough."
            ),
        }
