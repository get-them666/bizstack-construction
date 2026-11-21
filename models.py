"""Database models for Real-Time Quote Engine & Scheduling System."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, JSON,
    create_engine, event
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


# ─── Enums ───────────────────────────────────────────────────────────────────

class QuoteStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    AI_ANALYZED = "ai_analyzed"
    APPROVED = "approved"
    SENT = "sent"
    BOOKED = "booked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ConstructionProjectType(str, enum.Enum):
    KITCHEN = "kitchen"
    BATHROOM = "bathroom"
    WHOLE_HOME_REHAB = "whole_home_rehab"
    DRYWALL_PAINT = "drywall_paint"
    ROOFING_SIDING = "roofing_siding"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    CARPENTRY = "carpentry"
    FLOORING = "flooring"
    DECK_FENCE = "deck_fence"
    STR_MAKE_READY = "str_make_ready"


class FinishTier(str, enum.Enum):
    STANDARD = "standard"
    MID_RANGE = "mid_range"
    CUSTOM_LUXURY = "custom_luxury"


class LayoutType(str, enum.Enum):
    GALLEY = "galley"
    L_SHAPE = "l_shape"
    U_SHAPE = "u_shape"
    ISLAND = "island"
    OPEN = "open"
    EXTERIOR = "exterior"
    OTHER = "other"


class DemolitionComplexity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CleaningServiceType(str, enum.Enum):
    TURNOVER = "turnover"
    DEEP_CLEAN = "deep_clean"
    MOVE_OUT = "move_out"
    STANDARD = "standard"


# ─── Core Models ─────────────────────────────────────────────────────────────

class Quote(Base):
    """Base quote record shared by both brands."""
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True, index=True)
    brand = Column(String(20), nullable=False)  # "construction" or "broom"
    status = Column(Enum(QuoteStatus), default=QuoteStatus.DRAFT, nullable=False)
    
    # Customer info
    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False)
    customer_phone = Column(String(50), nullable=False)
    property_address = Column(Text, nullable=False)
    zip_code = Column(String(10), nullable=False)
    square_footage = Column(Float, nullable=True)
    
    # Construction-specific fields
    project_type = Column(String(100), nullable=True)  # kitchen, bathroom, whole_home_rehab, etc.
    finish_tier = Column(String(50), nullable=True)  # standard, mid_range, custom_luxury
    
    # Cleaning-specific fields
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Integer, nullable=True)
    service_type = Column(String(50), nullable=True)  # turnover, deep_clean, move_out
    add_ons = Column(JSON, nullable=True)  # List of add-on names
    
    # Quote metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    
    # AI analysis
    vision_analysis_id = Column(Integer, ForeignKey("vision_analyses.id"), nullable=True)
    
    # Pricing
    base_price = Column(Float, nullable=True)
    adjustments = Column(JSON, nullable=True)  # List of adjustment dicts
    final_price = Column(Float, nullable=True)
    
    # Calendar booking
    appointment_id = Column(Integer, ForeignKey("scheduled_appointments.id"), nullable=True)
    
    # Notes
    admin_notes = Column(Text, nullable=True)
    customer_notes = Column(Text, nullable=True)
    
    # Relationships
    photos = relationship("Photo", back_populates="quote", cascade="all, delete-orphan")
    vision_analysis = relationship("VisionAnalysis", back_populates="quote", uselist=False)
    appointment = relationship("ScheduledAppointment", back_populates="quote", uselist=False)
    pricing_rules_applied = relationship("QuotePricingRule", back_populates="quote", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Quote #{self.id} {self.brand} {self.status}>"


class Photo(Base):
    """Customer-uploaded photos for quote analysis."""
    __tablename__ = "photos"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False)
    
    # File info
    filename = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)  # Local filesystem path
    file_size = Column(Integer, nullable=False)  # bytes
    mime_type = Column(String(100), nullable=False)
    
    # Photo metadata
    room_type = Column(String(100), nullable=True)  # "kitchen", "bathroom", "living_room", etc.
    photo_order = Column(Integer, nullable=True)  # Sort order
    is_required = Column(Boolean, default=True, nullable=False)
    
    # AI analysis results
    vision_extracted = Column(JSON, nullable=True)  # Raw AI extraction for this photo
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    quote = relationship("Quote", back_populates="photos")

    def __repr__(self):
        return f"<Photo #{self.id} for Quote #{self.quote_id}>"


class VisionAnalysis(Base):
    """AI vision analysis results from photos."""
    __tablename__ = "vision_analyses"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False, unique=True)
    
    # AI provider
    provider = Column(String(50), nullable=False)  # "gemini" or "openai"
    model = Column(String(100), nullable=False)  # e.g., "gemini-pro-vision"
    
    # Analysis results (stored as JSON for flexibility)
    extracted_data = Column(JSON, nullable=False)  # Pydantic schema output
    raw_response = Column(Text, nullable=True)  # Raw AI response for debugging
    
    # Confidence metrics
    confidence_score = Column(Float, nullable=True)  # 0.0 to 1.0
    red_flags = Column(JSON, nullable=True)  # List of flagged issues
    
    # Processing metadata
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processing_time_seconds = Column(Float, nullable=True)
    
    # Relationships
    quote = relationship("Quote", back_populates="vision_analysis", uselist=False)

    def __repr__(self):
        return f"<VisionAnalysis #{self.id} for Quote #{self.quote_id}>"


class ScheduledAppointment(Base):
    """Calendar appointment from Cal.com or Easy!Appointments."""
    __tablename__ = "scheduled_appointments"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False, unique=True)
    
    # Appointment details
    appointment_type = Column(String(100), nullable=False)  # "walkthrough", "cleaning_slot", etc.
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    
    # Location
    location_type = Column(String(50), nullable=False)  # "on_site", "virtual", "phone"
    address = Column(Text, nullable=True)
    zone = Column(String(50), nullable=True)  # "core" or "extended"
    travel_buffer_minutes = Column(Integer, default=0, nullable=False)
    
    # Customer info (denormalized for quick access)
    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False)
    customer_phone = Column(String(50), nullable=False)
    
    # Calendar provider
    provider = Column(String(50), nullable=False)  # "cal_com" or "easy_appointments"
    external_id = Column(String(255), nullable=True)  # ID in external system
    external_url = Column(Text, nullable=True)  # Link to external calendar event
    
    # Status
    status = Column(String(50), default="confirmed", nullable=False)  # "confirmed", "cancelled", "completed"
    
    # Notes
    notes = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    quote = relationship("Quote", back_populates="appointment", uselist=False)

    def __repr__(self):
        return f"<Appointment #{self.id} for Quote #{self.quote_id}>"


class PricingRule(Base):
    """Admin-editable pricing rules for deterministic quote calculation."""
    __tablename__ = "pricing_rules"

    id = Column(Integer, primary_key=True, index=True)
    
    # Rule metadata
    brand = Column(String(20), nullable=False)  # "construction" or "broom"
    category = Column(String(100), nullable=False)  # "base_rate", "per_sqft", "add_on", "travel_fee", etc.
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    
    # Pricing values
    base_amount = Column(Float, nullable=False)  # Base price in dollars
    per_unit_amount = Column(Float, nullable=True)  # Per sqft/per bedroom/etc.
    multiplier = Column(Float, default=1.0, nullable=False)  # Tier multiplier
    
    # Applicability
    project_type = Column(String(100), nullable=True)  # Specific project type this applies to
    finish_tier = Column(String(50), nullable=True)  # "standard", "mid_range", "custom_luxury"
    zone = Column(String(50), nullable=True)  # "core" or "extended"
    
    # Active status
    is_active = Column(Boolean, default=True, nullable=False)
    effective_date = Column(DateTime, nullable=True)
    expiry_date = Column(DateTime, nullable=True)
    
    # Audit
    created_by = Column(String(255), nullable=True)
    updated_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<PricingRule #{self.id} {self.brand}/{self.category}: {self.name}>"


class QuotePricingRule(Base):
    """Links applied pricing rules to a specific quote (audit trail)."""
    __tablename__ = "quote_pricing_rules"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False)
    pricing_rule_id = Column(Integer, ForeignKey("pricing_rules.id"), nullable=False)
    
    # Applied values
    quantity = Column(Float, nullable=False)  # sqft, bedrooms, etc.
    unit_price = Column(Float, nullable=False)
    line_total = Column(Float, nullable=False)
    
    # Justification
    applied_reason = Column(Text, nullable=True)  # Why this rule was applied
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    quote = relationship("Quote", back_populates="pricing_rules_applied")
    pricing_rule = relationship("PricingRule")

    def __repr__(self):
        return f"<QuotePricingRule #{self.id} Quote #{self.quote_id} Rule #{self.pricing_rule_id}>"


# ─── Database Setup ──────────────────────────────────────────────────────────

def get_engine(database_url: str = "sqlite:///./quote_engine.db"):
    """Create database engine."""
    engine = create_engine(database_url, echo=False)
    
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        if database_url.startswith("sqlite"):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    
    return engine


def get_sessionmaker(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


def init_db(database_url: str = "sqlite:///./quote_engine.db"):
    """Initialize database tables."""
    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)
    return engine


# ─── ZIP Code Zone Logic ─────────────────────────────────────────────────────

CORE_ZONE_ZIPS = {
    # Chesapeake
    "23320", "23321", "23322", "23323", "23324", "23325", "23326", "23327", "23328",
    # Virginia Beach
    "23451", "23452", "23453", "23454", "23455", "23456", "23457", "23458", "23459", "23460", "23461", "23462", "23464", "23466", "23471",
    # Norfolk
    "23501", "23502", "23503", "23504", "23505", "23507", "23508", "23509", "23510", "23511", "23513", "23514", "23515", "23517", "23518", "23519",
    # Portsmouth
    "23701", "23702", "23703", "23704", "23707", "23708",
    # Suffolk
    "23432", "23433", "23434", "23435", "23436", "23437", "23438",
    # Newport News
    "23601", "23602", "23603", "23604", "23605", "23606", "23607", "23608",
    # Hampton
    "23661", "23662", "23663", "23664", "23665", "23666", "23667", "23668", "23669", "23670",
    # Williamsburg
    "23185", "23188",
    # Yorktown
    "23690", "23691", "23692", "23693",
}

EXTENDED_ZONE_ZIPS = {
    # Currituck County, NC
    "27916", "27921", "27923", "27924", "27926", "27927", "27929", "27932", "27939", "27949", "27958", "27960", "27962", "27964", "27966", "27973", "27974",
    # Elizabeth City, NC
    "27909", "27910",
    # Corolla / Outer Banks, NC
    "27927", "27947", "27959", "27968", "27976",
}


def get_zone(zip_code: str) -> str:
    """Determine pricing zone from ZIP code."""
    zip_clean = zip_code.strip()[:5]
    if zip_clean in CORE_ZONE_ZIPS:
        return "core"
    elif zip_clean in EXTENDED_ZONE_ZIPS:
        return "extended"
    else:
        return "extended"  # Default to extended for unknown ZIPs


def get_travel_buffer(zip_code: str) -> int:
    """Get travel buffer in minutes based on ZIP zone."""
    zone = get_zone(zip_code)
    return 0 if zone == "core" else 45


def get_travel_fee(zip_code: str, pricing_rules: list) -> float:
    """Calculate travel fee based on ZIP zone."""
    zone = get_zone(zip_code)
    for rule in pricing_rules:
        if rule.brand == "construction" and rule.category == "travel_fee" and rule.zone == zone:
            return rule.base_amount
    return 0.0  # No fee for core zone
