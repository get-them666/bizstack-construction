"""Ballpark estimating engine for instant quotes.

Cost models are price-per-unit ranges seeded from supplier price books
(Lowe's / ABC Supply pro pricing) and local labor rates. They produce a WIDE,
non-binding range intended only to qualify a lead — the fixed price always
comes from a written on-site estimate.

Unit costs are overridable via env so the owner can retune without code.
"""

import os


def _env_float(key, default):
    try:
        return float(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default


# --- price models ------------------------------------------------------------
# kind: "sqft"   => per interior sq ft
#       "square" => per roof square (100 sq ft)
#       "fixed"  => not sqft-driven
MODELS = {
    "whole-home": {
        "label": "Whole-Home Renovation",
        "kind": "sqft",
        "low": _env_float("EST_WHOLE_HOME_LOW", 95.0),
        "high": _env_float("EST_WHOLE_HOME_HIGH", 175.0),
        "unit": "interior sq ft",
    },
    "refresh": {
        "label": "Interior Refresh (paint, flooring, trim)",
        "kind": "sqft",
        "low": _env_float("EST_REFRESH_LOW", 12.0),
        "high": _env_float("EST_REFRESH_HIGH", 35.0),
        "unit": "interior sq ft",
    },
    "drywall": {
        "label": "Drywall & Paint",
        "kind": "sqft",
        "low": _env_float("EST_DRYWALL_LOW", 7.0),
        "high": _env_float("EST_DRYWALL_HIGH", 15.0),
        "unit": "interior sq ft",
    },
    "roof": {
        "label": "Roofing & Siding",
        "kind": "square",
        "low": _env_float("EST_ROOF_LOW", 650.0),
        "high": _env_float("EST_ROOF_HIGH", 1200.0),
        "unit": "roof square (100 sq ft)",
    },
    "kitchen": {
        "label": "Kitchen Remodel",
        "kind": "fixed",
        "low": _env_float("EST_KITCHEN_LOW", 18000.0),
        "high": _env_float("EST_KITCHEN_HIGH", 45000.0),
        "unit": "project",
    },
    "bath": {
        "label": "Bathroom Remodel",
        "kind": "fixed",
        "low": _env_float("EST_BATH_LOW", 9000.0),
        "high": _env_float("EST_BATH_HIGH", 25000.0),
        "unit": "project",
    },
    "fence": {
        "label": "Deck & Fence",
        "kind": "fixed",
        "low": _env_float("EST_FENCE_LOW", 2500.0),
        "high": _env_float("EST_FENCE_HIGH", 12000.0),
        "unit": "project",
    },
}

# project_type (as submitted on the site) -> model key
PROJECT_MODEL_MAP = {
    "whole-home": "whole-home",
    "Whole-Home Renovation": "whole-home",
    "Interior Refresh": "refresh",
    "Drywall & Paint": "drywall",
    "drywall": "drywall",
    "Roofing & Siding": "roof",
    "roof": "roof",
    "Kitchen": "kitchen",
    "kitchen": "kitchen",
    "Bathroom": "bath",
    "bathroom": "bath",
    "Deck & Fence": "fence",
    "deck": "fence",
    "Short-Term Rental Make-Ready": "refresh",
}

ROOF_PITCH_FACTOR = 1.25  # footprint -> roof area (includes pitch + fascia)


def project_model(project_type: str) -> str:
    return PROJECT_MODEL_MAP.get(project_type or "", "whole-home")


def roof_squares(interior_sqft: float, stories: int = 1) -> float:
    """Estimate roof squares from interior sqft (no native roof data exists)."""
    stories = max(int(stories or 1), 1)
    footprint = interior_sqft / stories
    roof_area = footprint * ROOF_PITCH_FACTOR
    return roof_area / 100.0


def _multipliers(property_data: dict):
    """Adjust for era, size bands, and condition that drove undervalue majors."""
    m = 1.0
    note = []
    year = int((property_data or {}).get("year_built") or 0)
    if year and year < 1970:
        m *= 1.15
        note.append("pre-1970 build (structural/upgrade risk)")
    elif year and year < 1990:
        m *= 1.08
        note.append("pre-1990 build")
    sqft = float((property_data or {}).get("sqft") or 0)
    if sqft and sqft < 1000:
        m *= 1.15
        note.append("small footprint (higher per-sqft cost)")
    elif sqft and sqft > 3000:
        m *= 0.95
        note.append("large footprint (efficiency)")
    return m, note


def estimate(project_type: str, property_data: dict | None = None, manual_sqft: float | None = None):
    """Compute a ballpark (low, high) in dollars from a model + property facts."""
    model_key = project_model(project_type)
    model = MODELS.get(model_key, MODELS["whole-home"])
    sqft = float(manual_sqft or 0)
    stories = 1
    if not sqft and property_data:
        sqft = float(property_data.get("sqft") or 0)
        stories = max(int(property_data.get("stories") or 1), 1)

    mult, notes = _multipliers(property_data or {})
    roof_sq = 0.0
    quantifier = None

    if model["kind"] == "sqft":
        if sqft <= 0:
            return None
        quantifier = f"{sqft:,.0f} interior sq ft"
        low = sqft * model["low"] * mult
        high = sqft * model["high"] * mult
    elif model["kind"] == "square":
        if sqft <= 0:
            return None
        roof_sq = roof_squares(sqft, stories)
        quantifier = f"~{roof_sq:,.0f} roof squares"
        low = roof_sq * model["low"] * mult
        high = roof_sq * model["high"] * mult
    else:
        quantifier = "1 project"
        low = model["low"] * mult
        high = model["high"] * mult

    if manual_sqft and manual_sqft > 0 and not sqft:
        sqft = float(manual_sqft)

    return {
        "model_key": model_key,
        "label": model["label"],
        "unit": model["unit"],
        "low_cents": int(round(low * 100)),
        "high_cents": int(round(high * 100)),
        "low_est": round(low),
        "high_est": round(high),
        "quantifier": quantifier,
        "sqft": round(sqft or 0),
        "roof_squares": round(roof_sq or 0, 1),
        "multipliers_applied": bool(mult != 1.0),
        "notes": notes,
        "disclaimer": (
            "This ballpark is generated from public property data and our price "
            "ranges. It is NOT a bid. Final pricing comes from a free on-site "
            "walkthrough and a written, fixed-price scope."
        ),
    }