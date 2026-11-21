"""Pydantic schemas for Vision AI structured outputs."""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# ─── Construction Vision Schemas ─────────────────────────────────────────────

class ConstructionLayoutType(str, Enum):
    GALLEY = "galley"
    L_SHAPE = "l_shape"
    U_SHAPE = "u_shape"
    ISLAND = "island"
    OPEN = "open"
    EXTERIOR = "exterior"
    OTHER = "other"


class ConstructionDemolitionComplexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConstructionRedFlag(str, Enum):
    WATER_DAMAGE = "water_damage"
    LOAD_BEARING_WALL = "load_bearing_wall"
    CAST_IRON_PLUMBING = "cast_iron_plumbing"
    OUTDATED_PANEL = "outdated_panel"
    SOFFIT_REMOVAL = "soffit_removal"
    SUBFLOOR_ROT = "subfloor_rot"
    ASBESTOS_RISK = "asbestos_risk"
    LEAD_PAINT_RISK = "lead_paint_risk"
    STRUCTURAL_ISSUE = "structural_issue"
    ELECTRICAL_HAZARD = "electrical_hazard"
    MOLD_DETECTED = "mold_detected"
    OTHER = "other"


class ConstructionVisionSchema(BaseModel):
    """AI vision extraction for construction project photos."""
    
    layout_type: ConstructionLayoutType = Field(
        ...,
        description="Room or project layout type",
        examples=["galley", "l_shape", "open"]
    )
    
    estimated_cabinet_linear_ft: float = Field(
        ...,
        description="Estimated linear feet of cabinetry (for kitchen/bath projects)",
        ge=0,
        examples=[12.5, 18.0, 0.0]
    )
    
    estimated_countertop_sqft: float = Field(
        ...,
        description="Estimated square footage of countertop area",
        ge=0,
        examples=[25.0, 40.0, 0.0]
    )
    
    existing_flooring: str = Field(
        ...,
        description="Type of existing flooring observed",
        examples=["hardwood", "tile", "carpet", "vinyl", "concrete"]
    )
    
    demolition_complexity: ConstructionDemolitionComplexity = Field(
        ...,
        description="Estimated complexity of demolition required"
    )
    
    red_flags: List[ConstructionRedFlag] = Field(
        default_factory=list,
        description="List of potential issues requiring inspection",
        max_length=20
    )
    
    confidence_score: float = Field(
        ...,
        description="AI confidence in analysis (0.0 to 1.0)",
        ge=0.0,
        le=1.0,
        examples=[0.92, 0.75, 0.45]
    )
    
    # Optional additional fields
    room_dimensions_estimate: Optional[str] = Field(
        None,
        description="Estimated room dimensions (e.g., '12x14 ft')"
    )
    
    wall_condition: Optional[str] = Field(
        None,
        description="Condition of walls (e.g., 'good', 'needs_repair', 'water_damage')"
    )
    
    ceiling_height_estimate: Optional[float] = Field(
        None,
        description="Estimated ceiling height in feet",
        ge=6.0,
        le=20.0
    )
    
    natural_light_rating: Optional[int] = Field(
        None,
        description="Natural light availability (1=poor, 5=excellent)",
        ge=1,
        le=5
    )
    
    @field_validator('confidence_score', mode='before')
    @classmethod
    def validate_confidence(cls, v):
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v


# ─── Cleaning Vision Schemas ─────────────────────────────────────────────────

class CleaningServiceLevel(str, Enum):
    LIGHT = "light"
    MODERATE = "moderate"
    HEAVY = "heavy"
    EXTREME = "extreme"


class CleaningVisionSchema(BaseModel):
    """AI vision extraction for cleaning service photos."""
    
    soil_and_clutter_level: int = Field(
        ...,
        description="Overall soil and clutter level (1=clean, 5=extreme)",
        ge=1,
        le=5,
        examples=[2, 4, 5]
    )
    
    glass_shower_doors: int = Field(
        ...,
        description="Number of glass shower doors observed",
        ge=0,
        examples=[0, 1, 2]
    )
    
    pet_hair_detected: bool = Field(
        ...,
        description="Whether pet hair was detected in photos"
    )
    
    recommended_deep_clean_upgrade: bool = Field(
        ...,
        description="Whether AI recommends upgrading to deep clean service"
    )
    
    confidence_score: float = Field(
        ...,
        description="AI confidence in analysis (0.0 to 1.0)",
        ge=0.0,
        le=1.0,
        examples=[0.95, 0.78, 0.60]
    )
    
    # Optional detailed fields
    service_level_assessment: Optional[CleaningServiceLevel] = Field(
        None,
        description="Overall service level needed"
    )
    
    estimated_cleaning_time_minutes: Optional[int] = Field(
        None,
        description="Estimated cleaning time in minutes",
        ge=30,
        le=600
    )
    
    special_attention_areas: Optional[List[str]] = Field(
        default_factory=list,
        description="Areas requiring special attention",
        max_length=20
    )
    
    appliance_count: Optional[dict] = Field(
        None,
        description="Count of major appliances (oven, fridge, microwave, etc.)"
    )
    
    bathroom_fixtures: Optional[int] = Field(
        None,
        description="Number of bathroom fixtures (toilets, sinks, tubs)",
        ge=0
    )
    
    floor_type_observed: Optional[str] = Field(
        None,
        description="Type of flooring observed",
        examples=["hardwood", "tile", "carpet", "vinyl"]
    )
    
    @field_validator('soil_and_clutter_level', mode='before')
    @classmethod
    def validate_soil_level(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return 3
        return v


# ─── Vision Analysis Request/Response Schemas ─────────────────────────────────

class VisionAnalysisRequest(BaseModel):
    """Request schema for vision analysis."""
    
    quote_id: int
    brand: str = Field(..., pattern="^(construction|broom)$")
    photo_paths: List[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="List of photo file paths to analyze"
    )
    
    # Optional context
    project_type: Optional[str] = None
    square_footage: Optional[float] = None
    additional_notes: Optional[str] = None


class VisionAnalysisResponse(BaseModel):
    """Response schema from vision analysis."""
    
    success: bool
    quote_id: int
    analysis: Optional[dict] = None
    raw_response: Optional[str] = None
    confidence_score: Optional[float] = None
    red_flags: Optional[List[str]] = None
    error_message: Optional[str] = None
    processing_time_seconds: Optional[float] = None


# ─── Pricing Engine Schemas ───────────────────────────────────────────────────

class PricingRequest(BaseModel):
    """Request schema for pricing calculation."""
    
    quote_id: int
    brand: str = Field(..., pattern="^(construction|broom)$")
    
    # Common fields
    square_footage: float = Field(..., ge=100, le=50000)
    zip_code: str = Field(..., min_length=5, max_length=5)
    
    # Construction-specific
    project_type: Optional[str] = None
    finish_tier: Optional[str] = Field(None, pattern="^(standard|mid_range|custom_luxury)$")
    vision_analysis: Optional[ConstructionVisionSchema] = None
    
    # Cleaning-specific
    bedrooms: Optional[int] = Field(None, ge=0, le=10)
    bathrooms: Optional[int] = Field(None, ge=1, le=10)
    service_type: Optional[str] = None
    add_ons: Optional[List[str]] = None
    cleaning_vision_analysis: Optional[CleaningVisionSchema] = None


class PricingLineItem(BaseModel):
    """Individual line item in a quote."""
    
    description: str
    quantity: float
    unit_price: float
    line_total: float
    pricing_rule_id: Optional[int] = None
    justification: Optional[str] = None


class PricingResponse(BaseModel):
    """Response schema from pricing calculation."""
    
    quote_id: int
    success: bool
    base_price: float
    adjustments: List[PricingLineItem]
    final_price: float
    confidence_score: Optional[float] = None
    red_flags: Optional[List[str]] = None
    requires_walkthrough: bool = False
    quote_type: str = Field(
        ...,
        pattern="^(instant_guaranteed|conditional_fixed|estimate_only)$"
    )
    error_message: Optional[str] = None
