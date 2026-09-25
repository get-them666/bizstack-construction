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
    "basement": {
        "label": "Basement Finishing",
        "kind": "sqft",
        "low": _env_float("EST_BASEMENT_LOW", 18.0),
        "high": _env_float("EST_BASEMENT_HIGH", 55.0),
        "unit": "interior sq ft",
    },
    "electrical": {
        "label": "Electrical Work",
        "kind": "fixed",
        "low": _env_float("EST_ELECTRICAL_LOW", 1500.0),
        "high": _env_float("EST_ELECTRICAL_HIGH", 8000.0),
        "unit": "project",
    },
    "plumbing": {
        "label": "Plumbing Work",
        "kind": "fixed",
        "low": _env_float("EST_PLUMBING_LOW", 1200.0),
        "high": _env_float("EST_PLUMBING_HIGH", 7500.0),
        "unit": "project",
    },
    "hvac": {
        "label": "HVAC / Heating & Cooling",
        "kind": "fixed",
        "low": _env_float("EST_HVAC_LOW", 3500.0),
        "high": _env_float("EST_HVAC_HIGH", 15000.0),
        "unit": "project",
    },
    "tile": {
        "label": "Tile Installation",
        "kind": "sqft",
        "low": _env_float("EST_TILE_LOW", 8.0),
        "high": _env_float("EST_TILE_HIGH", 20.0),
        "unit": "sq ft installed",
    },
    "carpet": {
        "label": "Carpet Installation",
        "kind": "sqft",
        "low": _env_float("EST_CARPET_LOW", 4.0),
        "high": _env_float("EST_CARPET_HIGH", 9.0),
        "unit": "sq yd installed",
    },
    "concrete": {
        "label": "Concrete Work",
        "kind": "fixed",
        "low": _env_float("EST_CONCRETE_LOW", 3000.0),
        "high": _env_float("EST_CONCRETE_HIGH", 15000.0),
        "unit": "project",
    },
    "masonry": {
        "label": "Masonry / Brick & Stone",
        "kind": "fixed",
        "low": _env_float("EST_MASONRY_LOW", 2500.0),
        "high": _env_float("EST_MASONRY_HIGH", 12000.0),
        "unit": "project",
    },
    "framing": {
        "label": "Framing / Structure",
        "kind": "sqft",
        "low": _env_float("EST_FRAMING_LOW", 10.0),
        "high": _env_float("EST_FRAMING_HIGH", 25.0),
        "unit": "sq ft framed",
    },
    "carpentry": {
        "label": "Carpentry / Woodwork",
        "kind": "fixed",
        "low": _env_float("EST_CARPENTRY_LOW", 1500.0),
        "high": _env_float("EST_CARPENTRY_HIGH", 10000.0),
        "unit": "project",
    },
    "trim": {
        "label": "Trim & Molding",
        "kind": "sqft",
        "low": _env_float("EST_TRIM_LOW", 4.0),
        "high": _env_float("EST_TRIM_HIGH", 10.0),
        "unit": "sq ft of trim",
    },
}

# project_type (as submitted on the site) -> model key
PROJECT_MODEL_MAP = {
    "whole-home": "whole-home",
    "Whole-Home Renovation": "whole-home",
    "Whole-home renovation": "whole-home",
    "whole home": "whole-home",
    "whole-house": "whole-home",
    "new construction": "whole-home",
    "Additions": "whole-home",
    "addition": "whole-home",
    "Garage / ADU": "whole-home",
    "garage": "whole-home",
    "adu": "whole-home",
    "Foundation / structural": "whole-home",
    "foundation": "whole-home",
    "structural": "whole-home",
    "Interior Refresh": "refresh",
    "Flooring": "refresh",
    "Painting": "refresh",
    "paint": "refresh",
    "floors": "refresh",
    "flooring": "refresh",
    "Drywall & Paint": "drywall",
    "drywall": "drywall",
    "sheetrock": "drywall",
    "Roofing & Siding": "roof",
    "Roofing": "roof",
    "Roof": "roof",
    "roof": "roof",
    "roofing": "roof",
    "roofs": "roof",
    "shingles": "roof",
    "siding": "roof",
    "gutters": "roof",
    "Siding / windows": "roof",
    "windows": "roof",
    "Kitchen": "kitchen",
    "Kitchen remodel": "kitchen",
    "kitchen": "kitchen",
    "kitchens": "kitchen",
    "kitchen remodel": "kitchen",
    "cabinets": "kitchen",
    "countertops": "kitchen",
    "Bathroom": "bath",
    "Bathroom remodel": "bath",
    "bathroom": "bath",
    "bath": "bath",
    "bathrooms": "bath",
    "baths": "bath",
    "bathroom remodel": "bath",
    "Deck & Fence": "fence",
    "Deck / porch": "fence",
    "Deck": "fence",
    "Fence": "fence",
    "deck": "fence",
    "fence": "fence",
    "decks": "fence",
    "fences": "fence",
    "patio": "fence",
    "porch": "fence",
    "Short-Term Rental Make-Ready": "refresh",
    "basement": "basement",
    "Basement": "basement",
    "basement finishing": "basement",
    "electrical": "electrical",
    "Electrical": "electrical",
    "electrician": "electrical",
    "electrical panel": "electrical",
    "plumbing": "plumbing",
    "Plumbing": "plumbing",
    "plumber": "plumbing",
    "pipe": "plumbing",
    "hvac": "hvac",
    "HVAC": "hvac",
    "furnace": "hvac",
    "ac": "hvac",
    "heating": "hvac",
    "air conditioning": "hvac",
    "tile": "tile",
    "Tile": "tile",
    "tiling": "tile",
    "carpet": "carpet",
    "Carpet": "carpet",
    "carpeting": "carpet",
    "concrete": "concrete",
    "Concrete": "concrete",
    "cement": "concrete",
    "concrete driveway": "concrete",
    "masonry": "masonry",
    "Masonry": "masonry",
    "brick": "masonry",
    "stone": "masonry",
    "framing": "framing",
    "Framing": "framing",
    "carpentry": "carpentry",
    "Carpentry": "carpentry",
    "woodwork": "carpentry",
    "trim": "trim",
    "Trim": "trim",
    "baseboards": "trim",
    "molding": "trim",
}

ROOF_PITCH_FACTOR = 1.25  # footprint -> roof area (includes pitch + fascia)

# --- Material-grade modifiers -------------------------------------------------
# Multipliers applied ON TOP of the base ranges when the caller names a
# specific grade. Each group holds key->(multiplier, label, applies_to). As the
# owner asks the caller what grade they want (3-tab vs architectural shingles,
# laminate vs marble countertops, standard vs premium lumber, etc.), the bot
# passes those choices through and the quote narrows accordingly.
MATERIAL_GRADES = {
    "shingles": {
        "3-tab": (0.80, "3-tab shingles", ("roof",)),
        "three-tab": (0.80, "3-tab shingles", ("roof",)),
        "architectural": (1.00, "architectural shingles", ("roof",)),
        "20-year": (0.95, "20-year shingles", ("roof",)),
        "30-year": (1.05, "30-year architectural shingles", ("roof",)),
        "40-year": (1.20, "40-year premium shingles", ("roof",)),
        "50-year": (1.30, "50-year premium shingles", ("roof",)),
        "metal": (1.80, "standing-seam metal roof", ("roof",)),
    },
    "countertop": {
        "laminate": (0.85, "laminate countertops", ("kitchen", "bath")),
        "formica": (0.85, "laminate countertops", ("kitchen", "bath")),
        "tile": (0.95, "ceramic-tile countertops", ("kitchen", "bath")),
        "solid surface": (1.00, "solid-surface countertops", ("kitchen", "bath")),
        "granite": (1.10, "granite countertops", ("kitchen", "bath")),
        "quartz": (1.15, "quartz countertops", ("kitchen", "bath")),
        "marble": (1.35, "marble countertops", ("kitchen", "bath")),
        "butcher block": (1.05, "butcher-block countertops", ("kitchen", "bath")),
        "concrete": (1.20, "poured-concrete countertops", ("kitchen", "bath")),
    },
    "lumber": {
        "standard": (1.00, "standard lumber", ("whole-home", "refresh", "deck/fence", "fence")),
        "spf": (1.00, "standard SPF lumber", ("whole-home", "refresh", "deck/fence", "fence")),
        "premium": (1.15, "premium/select lumber", ("whole-home", "refresh", "deck/fence", "fence")),
        "select": (1.15, "premium/select lumber", ("whole-home", "refresh", "deck/fence", "fence")),
        "engineered": (1.20, "engineered lumber (LVL/I-joist)", ("whole-home", "refresh", "deck/fence", "fence")),
        "cedar": (1.60, "cedar (deck/fence)", ("deck/fence", "fence")),
        "redwood": (1.60, "redwood (deck/fence)", ("deck/fence", "fence")),
        "composite": (1.50, "composite decking (e.g. Trex)", ("deck/fence", "fence")),
        "trex": (1.50, "composite decking (e.g. Trex)", ("deck/fence", "fence")),
    },
}

# Keywords discovered -> which material grade group they belong to.
_MATERIAL_KEYWORD_GROUPS = {
    "shingle": "shingles", "3-tab": "shingles", "architectural": "shingles",
    "20-year": "shingles", "30-year": "shingles", "40-year": "shingles",
    "50-year": "shingles", "metal roof": "shingles", "standing seam": "shingles",
    "countertop": "countertop", "granite": "countertop", "quartz": "countertop",
    "marble": "countertop", "laminate": "countertop", "formica": "countertop",
    "butcher block": "countertop", "butcherblock": "countertop", "concrete": "countertop",
    "lumber": "lumber", "premium": "lumber", "select": "lumber",
    "engineered": "lumber", "cedar": "lumber", "redwood": "lumber",
    "composite": "lumber", "trex": "lumber",
}


def material_multipliers(materials: str | list | None, model_key: str | None = None):
    """Scan free-text material choices and return (multiplier, list_of_labels).

    Only applies options relevant to the project type (e.g. marble doesn't bump
    a roof quote). model_key may be omitted to take everything. Within a group
    the longest matching phrase wins, so '30-year' beats 'architectural' when
    both appear in e.g. 'architectural 30-year shingles'."""
    if isinstance(materials, str):
        text = (" " + materials + " ").lower()
    elif isinstance(materials, list):
        text = " " + " ".join(str(x) for x in materials) + " "
    else:
        text = ""
    m = 1.0
    labels = []
    for group, options in MATERIAL_GRADES.items():
        best_mult, best_label, best_key = None, None, ""
        for opt_key, (mult, label, applies_to) in options.items():
            if opt_key not in text:
                continue
            if model_key is not None and model_key not in applies_to:
                continue
            if best_mult is None or mult > best_mult or (mult == best_mult and len(opt_key) > len(best_key)):
                best_mult, best_label, best_key = mult, label, opt_key
        if best_mult is not None:
            m *= best_mult
            labels.append(best_label)
    return m, labels


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


def estimate(project_type: str, property_data: dict | None = None, manual_sqft: float | None = None,
             materials: str | list | None = None):
    """Compute a ballpark (low, high) in dollars from a model + property facts.

    `materials` is optional free-text/list of material-grade choices (e.g.
    "architectural 30-year shingles, marble countertops, premium lumber") and is
    applied on top of the base + property multipliers."""
    model_key = project_model(project_type)
    model = MODELS.get(model_key, MODELS["whole-home"])
    sqft = float(manual_sqft or 0)
    stories = 1
    if not sqft and property_data:
        sqft = float(property_data.get("sqft") or 0)
        stories = max(int(property_data.get("stories") or 1), 1)

    base_mult, notes = _multipliers(property_data or {})
    mat_mult, mat_labels = material_multipliers(materials, model_key)
    mult = base_mult * mat_mult
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
        "material_multipliers_applied": bool(mat_mult != 1.0),
        "material_notes": mat_labels,
        "notes": notes,
        "disclaimer": (
            "This ballpark is generated from public property data and our price "
            "ranges. It is NOT a bid. Final pricing comes from a free on-site "
            "walkthrough and a written, fixed-price scope."
        ),
    }


DEFAULT_QUOTE_SQFT = 1750.0


def auto_quote(project_type: str, sqft: float | None = None):
    """Quick ballpark range for auto-replies (no property lookup).

    Fixed-price models (kitchen, bath, deck/fence) quote directly. Per-sqft models
    assume a typical ~1,750 sq ft home when square footage isn't known, so a lead
    always gets an answer, clearly labeled as an assumption."""
    try:
        sqft = max(float(sqft or 0), 0.0)
    except (TypeError, ValueError):
        sqft = 0.0
    assumed = sqft <= 0 and project_model(project_type) in ("whole-home", "refresh", "drywall", "roof")
    if sqft <= 0:
        sqft = DEFAULT_QUOTE_SQFT
    est = estimate(project_type, None, sqft)
    if est is None:
        return None
    est["assumed_sqft"] = assumed
    est["kind"] = (MODELS.get(est.get("model_key") or "", MODELS["whole-home"]) or {}).get("kind", "fixed")
    return est