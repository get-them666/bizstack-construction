"""Deterministic Pricing Engine - Calculate quotes using admin-editable rules."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from models import (
    Quote,
    PricingRule,
    QuotePricingRule,
    get_zone,
    get_travel_buffer,
    get_travel_fee,
)
from schemas import (
    ConstructionVisionSchema,
    CleaningVisionSchema,
    PricingRequest,
    PricingResponse,
    PricingLineItem,
)


# ─── Base Rates (Fallback Defaults) ──────────────────────────────────────────

# These are used if no PricingRule records exist in the database
DEFAULT_PRICING_RULES = {
    "construction": {
        "base_rate": {
            "kitchen": 150.00,  # per sqft
            "bathroom": 125.00,
            "whole_home_rehab": 95.00,
            "drywall_paint": 8.00,
            "roofing_siding": 7.50,
            "plumbing": 135.00,  # per hour
            "electrical": 135.00,
            "carpentry": 100.00,
            "flooring": 12.00,  # per sqft installed
            "deck_fence": 85.00,  # per linear ft
            "str_make_ready": 95.00,
        },
        "tier_multipliers": {
            "standard": 1.0,
            "mid_range": 1.25,
            "custom_luxury": 1.6,
        },
        "travel_fees": {
            "core": 0.00,
            "extended": 75.00,
        },
        "demolition_rates": {
            "low": 5.00,  # per sqft
            "medium": 8.50,
            "high": 14.00,
        },
        "cabinet_rates": {
            "standard": 125.00,  # per linear ft
            "mid_range": 225.00,
            "custom_luxury": 450.00,
        },
        "countertop_rates": {
            "laminate": 45.00,  # per sqft
            "quartz": 95.00,
            "granite": 85.00,
            "marble": 125.00,
        },
    },
    "broom": {
        "base_rate": 150.00,  # base cleaning fee
        "per_bedroom": 35.00,
        "per_bathroom": 45.00,
        "per_sqft": 0.15,  # additional sqft adjustment
        "deep_clean_multiplier": 1.3,
        "move_out_multiplier": 1.2,
        "add_ons": {
            "inside_oven": 35.00,
            "inside_fridge": 30.00,
            "inside_microwave": 20.00,
            "interior_windows": 50.00,
            "blinds_shutters": 45.00,
            "baseboards": 50.00,
            "patio_balcony": 40.00,
            "bbq_grill": 45.00,
            "inside_cabinets": 50.00,
            "extra_bathroom": 40.00,
            "rush_service": 75.00,
            "heavy_pet_hair": 0.20,  # 20% surcharge
        },
    },
}


# ─── Construction Pricing Engine ─────────────────────────────────────────────

def calculate_construction_price(
    request: PricingRequest,
    db_session,
) -> PricingResponse:
    """Calculate construction quote using deterministic pricing rules."""
    
    # Load pricing rules from database or use defaults
    rules = _load_pricing_rules(db_session, "construction")
    base_rates = rules.get("base_rate", DEFAULT_PRICING_RULES["construction"]["base_rate"])
    tier_multipliers = rules.get("tier_multipliers", DEFAULT_PRICING_RULES["construction"]["tier_multipliers"])
    travel_fees = rules.get("travel_fees", DEFAULT_PRICING_RULES["construction"]["travel_fees"])
    demolition_rates = rules.get("demolition_rates", DEFAULT_PRICING_RULES["construction"]["demolition_rates"])
    cabinet_rates = rules.get("cabinet_rates", DEFAULT_PRICING_RULES["construction"]["cabinet_rates"])
    countertop_rates = rules.get("countertop_rates", DEFAULT_PRICING_RULES["construction"]["countertop_rates"])
    
    # Get tier multiplier
    finish_tier = request.finish_tier or "standard"
    tier_multiplier = tier_multipliers.get(finish_tier, 1.0)
    
    # Get zone and travel fee
    zone = get_zone(request.zip_code)
    travel_fee = travel_fees.get(zone, 0.0)
    travel_buffer = get_travel_buffer(request.zip_code)
    
    # Base calculation
    project_type = request.project_type or "whole_home_rehab"
    base_rate_per_sqft = base_rates.get(project_type, 95.00)
    base_price = request.square_footage * base_rate_per_sqft * tier_multiplier
    
    line_items: List[PricingLineItem] = []
    
    # Add base labor line
    line_items.append(PricingLineItem(
        description=f"{project_type.replace('_', ' ').title()} labor ({request.square_footage} sqft × ${base_rate_per_sqft}/sqft × {tier_multiplier}x tier)",
        quantity=request.square_footage,
        unit_price=base_rate_per_sqft * tier_multiplier,
        line_total=base_price,
        justification="Base labor calculation",
    ))
    
    # Add vision-based adjustments
    vision = request.vision_analysis
    if vision and isinstance(vision, ConstructionVisionSchema):
        
        # Demolition complexity adjustment
        if vision.demolition_complexity.value != "low":
            demo_rate = demolition_rates.get(vision.demolition_complexity.value, 8.50)
            demo_cost = request.square_footage * demo_rate
            line_items.append(PricingLineItem(
                description=f"Demolition ({vision.demolition_complexity.value} complexity)",
                quantity=request.square_footage,
                unit_price=demo_rate,
                line_total=demo_cost,
                justification=f"AI-detected {vision.demolition_complexity.value} demolition complexity",
            ))
            base_price += demo_cost
        
        # Cabinet linear footage
        if vision.estimated_cabinet_linear_ft > 0:
            cabinet_rate = cabinet_rates.get(finish_tier, 225.00)
            cabinet_cost = vision.estimated_cabinet_linear_ft * cabinet_rate
            line_items.append(PricingLineItem(
                description=f"Cabinetry ({vision.estimated_cabinet_linear_ft} linear ft)",
                quantity=vision.estimated_cabinet_linear_ft,
                unit_price=cabinet_rate,
                line_total=cabinet_cost,
                justification="AI-estimated cabinet linear footage from photos",
            ))
            base_price += cabinet_cost
        
        # Countertop square footage
        if vision.estimated_countertop_sqft > 0:
            # Default to mid-range countertop
            countertop_rate = countertop_rates.get("quartz", 95.00)
            countertop_cost = vision.estimated_countertop_sqft * countertop_rate
            line_items.append(PricingLineItem(
                description=f"Countertops ({vision.estimated_countertop_sqft} sqft)",
                quantity=vision.estimated_countertop_sqft,
                unit_price=countertop_rate,
                line_total=countertop_cost,
                justification="AI-estimated countertop area from photos",
            ))
            base_price += countertop_cost
        
        # Red flags require walkthrough
        requires_walkthrough = len(vision.red_flags) > 0
        confidence_score = vision.confidence_score
    else:
        requires_walkthrough = False
        confidence_score = None
    
    # Add travel fee if extended zone
    if travel_fee > 0:
        line_items.append(PricingLineItem(
            description=f"Travel fee ({zone} zone - {travel_buffer} min buffer)",
            quantity=1,
            unit_price=travel_fee,
            line_total=travel_fee,
            justification=f"Property located in {zone} zone (ZIP: {request.zip_code})",
        ))
        base_price += travel_fee
    
    # Determine quote type
    if confidence_score is not None:
        if confidence_score >= 0.80 and not requires_walkthrough:
            quote_type = "conditional_fixed"
        else:
            quote_type = "estimate_only"
    else:
        quote_type = "estimate_only"
    
    return PricingResponse(
        quote_id=request.quote_id,
        success=True,
        base_price=base_price,
        adjustments=line_items,
        final_price=base_price,
        confidence_score=confidence_score,
        red_flags=vision.red_flags if vision else [],
        requires_walkthrough=requires_walkthrough,
        quote_type=quote_type,
    )


# ─── Cleaning Pricing Engine ─────────────────────────────────────────────────

def calculate_cleaning_price(
    request: PricingRequest,
    db_session,
) -> PricingResponse:
    """Calculate cleaning quote using deterministic pricing rules."""
    
    # Load pricing rules from database or use defaults
    rules = _load_pricing_rules(db_session, "broom")
    
    base_rate = rules.get("base_rate", DEFAULT_PRICING_RULES["broom"]["base_rate"])
    per_bedroom = rules.get("per_bedroom", DEFAULT_PRICING_RULES["broom"]["per_bedroom"])
    per_bathroom = rules.get("per_bathroom", DEFAULT_PRICING_RULES["broom"]["per_bathroom"])
    per_sqft = rules.get("per_sqft", DEFAULT_PRICING_RULES["broom"]["per_sqft"])
    deep_clean_multiplier = rules.get("deep_clean_multiplier", DEFAULT_PRICING_RULES["broom"]["deep_clean_multiplier"])
    move_out_multiplier = rules.get("move_out_multiplier", DEFAULT_PRICING_RULES["broom"]["move_out_multiplier"])
    add_ons = rules.get("add_ons", DEFAULT_PRICING_RULES["broom"]["add_ons"])
    
    # Base calculation
    bedrooms = request.bedrooms or 1
    bathrooms = request.bathrooms or 1
    
    base_price = base_rate
    base_price += bedrooms * per_bedroom
    base_price += bathrooms * per_bathroom
    
    # Square footage adjustment (if over 1500 sqft)
    sqft_adjustment = 0.0
    if request.square_footage and request.square_footage > 1500:
        sqft_adjustment = (request.square_footage - 1500) * per_sqft
        base_price += sqft_adjustment
    
    line_items: List[PricingLineItem] = []
    
    # Add base labor line
    line_items.append(PricingLineItem(
        description=f"Base cleaning ({bedrooms} BR / {bathrooms} BA)",
        quantity=1,
        unit_price=base_rate + (bedrooms * per_bedroom) + (bathrooms * per_bathroom),
        line_total=base_rate + (bedrooms * per_bedroom) + (bathrooms * per_bathroom),
        justification="Standard base rate",
    ))
    
    if sqft_adjustment > 0:
        line_items.append(PricingLineItem(
            description=f"Square footage adjustment ({request.square_footage} sqft - 1500 base)",
            quantity=request.square_footage - 1500,
            unit_price=per_sqft,
            line_total=sqft_adjustment,
            justification="Additional sqft over 1500 base",
        ))
    
    # Service type multiplier
    service_type = request.service_type or "turnover"
    multiplier = 1.0
    if service_type == "deep_clean":
        multiplier = deep_clean_multiplier
        line_items.append(PricingLineItem(
            description="Deep clean upgrade (+30%)",
            quantity=1,
            unit_price=base_price * (deep_clean_multiplier - 1),
            line_total=base_price * (deep_clean_multiplier - 1),
            justification="Deep clean service selected",
        ))
    elif service_type == "move_out":
        multiplier = move_out_multiplier
        line_items.append(PricingLineItem(
            description="Move-out cleaning (+20%)",
            quantity=1,
            unit_price=base_price * (move_out_multiplier - 1),
            line_total=base_price * (move_out_multiplier - 1),
            justification="Move-out service selected",
        ))
    
    base_price *= multiplier
    
    # Add-ons
    add_on_total = 0.0
    if request.add_ons:
        for add_on in request.add_ons:
            if add_on in add_ons:
                cost = add_ons[add_on]
                # Handle percentage-based add-ons
                if isinstance(cost, float) and cost < 1.0:
                    cost = base_price * cost
                add_on_total += cost
                line_items.append(PricingLineItem(
                    description=f"Add-on: {add_on.replace('_', ' ').title()}",
                    quantity=1,
                    unit_price=cost,
                    line_total=cost,
                    justification=f"Customer-selected add-on",
                ))
    
    base_price += add_on_total
    
    # Vision-based adjustments
    vision = request.cleaning_vision_analysis
    requires_deep_clean = False
    confidence_score = None
    
    if vision and isinstance(vision, CleaningVisionSchema):
        confidence_score = vision.confidence_score
        
        # Auto-include deep clean if soil level >= 4
        if vision.soil_and_clutter_level >= 4 and "deep_clean" not in (request.add_ons or []):
            deep_clean_cost = base_price * 0.30
            line_items.append(PricingLineItem(
                description="AI-detected deep clean required (+30%)",
                quantity=1,
                unit_price=deep_clean_cost,
                line_total=deep_clean_cost,
                justification=f"Photos show soil level {vision.soil_and_clutter_level}/5 - deep clean recommended",
            ))
            base_price += deep_clean_cost
            requires_deep_clean = True
        
        # Pet hair surcharge
        if vision.pet_hair_detected and "heavy_pet_hair" not in (request.add_ons or []):
            pet_hair_cost = base_price * 0.20
            line_items.append(PricingLineItem(
                description="Pet hair treatment (+20%)",
                quantity=1,
                unit_price=pet_hair_cost,
                line_total=pet_hair_cost,
                justification="AI-detected pet hair in photos",
            ))
            base_price += pet_hair_cost
    
    # Determine quote type
    # Cleaning quotes are always instant guaranteed if confidence >= 0.7
    if confidence_score is not None and confidence_score >= 0.7:
        quote_type = "instant_guaranteed"
    else:
        quote_type = "instant_guaranteed"  # Cleaning is always guaranteed
    
    return PricingResponse(
        quote_id=request.quote_id,
        success=True,
        base_price=base_price,
        adjustments=line_items,
        final_price=base_price,
        confidence_score=confidence_score,
        red_flags=[],
        requires_walkthrough=False,
        quote_type=quote_type,
    )


# ─── Helper Functions ────────────────────────────────────────────────────────

def _load_pricing_rules(db_session, brand: str) -> dict:
    """Load pricing rules from database, organized by category."""
    try:
        stmt = select(PricingRule).where(
            PricingRule.brand == brand,
            PricingRule.is_active == True,
        )
        rules = db_session.execute(stmt).scalars().all()
        
        # Organize by category
        organized = {}
        for rule in rules:
            if rule.category not in organized:
                organized[rule.category] = {}
            
            # Store as key-value or nested dict
            if rule.project_type:
                if rule.category not in organized:
                    organized[rule.category] = {}
                organized[rule.category][rule.project_type] = rule.base_amount
            elif rule.finish_tier:
                organized[rule.category][rule.finish_tier] = rule.base_amount
            elif rule.zone:
                organized[rule.category][rule.zone] = rule.base_amount
            else:
                organized[rule.category][rule.name] = rule.base_amount
        
        return organized
    except Exception:
        return {}


def save_quote_pricing_rules(
    db_session,
    quote_id: int,
    line_items: List[PricingLineItem],
):
    """Save applied pricing rules to database for audit trail."""
    for item in line_items:
        quote_rule = QuotePricingRule(
            quote_id=quote_id,
            pricing_rule_id=item.pricing_rule_id or 0,
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=item.line_total,
            applied_reason=item.justification,
        )
        db_session.add(quote_rule)
    
    db_session.commit()
