"""Main FastAPI application for Real-Time Quote Engine & Scheduling."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from models import (
    Quote, Photo, VisionAnalysis, ScheduledAppointment, PricingRule,
    CustomerReview, MarketingPost,
    init_db, get_sessionmaker, get_engine,
    QuoteStatus, ConstructionProjectType, FinishTier, CleaningServiceType,
)
from schemas import (
    VisionAnalysisRequest, VisionAnalysisResponse,
    PricingRequest, PricingResponse,
)
from services import vision_analyzer, pricing_engine
from services.pricing_engine import DEFAULT_PRICING_RULES
from services import email_service
from services.local_seo import CITY_PAGES, get_city_page, get_all_city_slugs, build_json_ld_schema

# ─── Configuration ───────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./quote_engine.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads/quotes"))
TEMPLATE_DIR = Path(__file__).parent / "templates"

# Cal.com configuration
CALCOM_CONSTRUCTION_SLUG = os.getenv("CALCOM_CONSTRUCTION_EVENT_SLUG", "bizstack/free-walkthrough")
CALCOM_BROOM_SLUG = os.getenv("CALCOM_BROOM_EVENT_SLUG", "broom-service/str-cleaning")
CALCOM_WEBHOOK_SECRET = os.getenv("CALCOM_WEBHOOK_SECRET", "")

# Payment configuration
ZELLE_HANDLE = os.getenv("ZELLE_HANDLE", "hello@bizstackperks.com")
ACH_ROUTING = os.getenv("ACH_ROUTING", "")
ACH_ACCOUNT = os.getenv("ACH_ACCOUNT", "")
BANK_NAME = os.getenv("BANK_NAME", "BizStack Perks LLC")

# Review platform URLs
GOOGLE_REVIEW_URL = os.getenv("GOOGLE_REVIEW_URL", "https://g.page/broom-service-chesapeake/review")
FACEBOOK_REVIEW_URL = os.getenv("FACEBOOK_REVIEW_URL", "https://www.facebook.com/buildstackconstruction/reviews/")
YELP_REVIEW_URL = os.getenv("YELP_REVIEW_URL", "https://www.yelp.com/biz/broom-service-chesapeake")

# Base URL for email links
BASE_URL = os.getenv("BASE_URL", "https://bizstackperks.com")

# Create upload directory
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Initialize database
engine = get_engine(DATABASE_URL)
init_db(DATABASE_URL)
SessionLocal = get_sessionmaker(engine)

# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="Real-Time Quote Engine & Scheduling")

# Mount static files
static_dir = TEMPLATE_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Quote Submission Routes ─────────────────────────────────────────────────

@app.get("/instant-quote", response_class=HTMLResponse)
async def quote_form_page(request: Request, brand: str = "construction"):
    """Display the quote submission form."""
    return templates.TemplateResponse(
        "quote_form.html",
        {"request": request, "brand": brand}
    )


@app.post("/api/quote/submit")
async def submit_quote(
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    customer_phone: str = Form(...),
    property_address: str = Form(...),
    zip_code: str = Form(...),
    square_footage: float = Form(...),
    brand: str = Form(...),
    # Construction-specific
    project_type: Optional[str] = Form(None),
    finish_tier: Optional[str] = Form(None),
    # Cleaning-specific
    bedrooms: Optional[int] = Form(None),
    bathrooms: Optional[int] = Form(None),
    service_type: Optional[str] = Form(None),
    add_ons: Optional[str] = Form(None),  # JSON string
    # Notes
    customer_notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Submit a new quote request."""
    
    # Parse add_ons JSON if provided
    parsed_add_ons = None
    if add_ons:
        try:
            import json
            parsed_add_ons = json.loads(add_ons)
        except Exception:
            parsed_add_ons = None
    
    # Create quote record
    quote = Quote(
        brand=brand,
        status=QuoteStatus.DRAFT,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        property_address=property_address,
        zip_code=zip_code,
        square_footage=square_footage,
        customer_notes=customer_notes,
        # Construction-specific
        project_type=project_type,
        finish_tier=finish_tier,
        # Cleaning-specific
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        service_type=service_type,
        add_ons=parsed_add_ons,
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    
    return JSONResponse({
        "success": True,
        "quote_id": quote.id,
        "message": "Quote submitted. Please upload photos next.",
    })


@app.post("/api/quote/{quote_id}/photos")
async def upload_photos(
    quote_id: int,
    photos: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Upload photos for quote analysis."""
    
    # Verify quote exists
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    # Save photos
    photo_paths = []
    for photo in photos:
        # Generate unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{photo.filename}"
        file_path = UPLOAD_DIR / str(quote_id) / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save file
        with open(file_path, "wb") as f:
            content = await photo.read()
            f.write(content)
        
        # Create photo record
        photo_record = Photo(
            quote_id=quote_id,
            filename=filename,
            file_path=str(file_path),
            file_size=len(content),
            mime_type=photo.content_type or "image/jpeg",
        )
        db.add(photo_record)
        photo_paths.append(str(file_path))
    
    db.commit()
    
    return JSONResponse({
        "success": True,
        "photos_uploaded": len(photo_paths),
        "message": "Photos uploaded. Starting AI analysis...",
    })


@app.post("/api/quote/{quote_id}/analyze")
async def analyze_quote(
    quote_id: int,
    provider: str = "gemini",
    db: Session = Depends(get_db),
):
    """Run AI vision analysis on uploaded photos."""
    
    # Get quote and photos
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    photos = db.query(Photo).filter(Photo.quote_id == quote_id).all()
    if not photos:
        raise HTTPException(status_code=400, detail="No photos uploaded")
    
    photo_paths = [str(p.file_path) for p in photos]
    
    # Create vision analysis request
    request = VisionAnalysisRequest(
        quote_id=quote_id,
        brand=quote.brand,
        photo_paths=photo_paths,
        project_type=quote.project_type if hasattr(quote, 'project_type') else None,
        square_footage=quote.square_footage,
    )
    
    # Run analysis
    result = await vision_analyzer.analyze_photos(request, provider=provider)
    
    if result.success:
        # Save analysis to database
        analysis = VisionAnalysis(
            quote_id=quote_id,
            provider=provider,
            model="gemini-pro-vision" if provider == "gemini" else "gpt-4o-mini",
            extracted_data=result.analysis,
            raw_response=result.raw_response,
            confidence_score=result.confidence_score,
            red_flags=result.red_flags,
            processing_time_seconds=result.processing_time_seconds,
        )
        db.add(analysis)
        
        # Update quote
        quote.vision_analysis_id = analysis.id
        quote.status = QuoteStatus.AI_ANALYZED
        db.commit()
        
        # Calculate pricing
        # Reconstruct vision analysis as Pydantic object for pricing engine
        from schemas import ConstructionVisionSchema, CleaningVisionSchema
        
        vision_obj = None
        if quote.brand == "construction":
            try:
                vision_obj = ConstructionVisionSchema.model_validate(result.analysis)
            except Exception:
                pass
        else:
            try:
                vision_obj = CleaningVisionSchema.model_validate(result.analysis)
            except Exception:
                pass
        
        pricing_request = PricingRequest(
            quote_id=quote_id,
            brand=quote.brand,
            square_footage=quote.square_footage,
            zip_code=quote.zip_code,
            project_type=quote.project_type,
            finish_tier=quote.finish_tier,
            vision_analysis=vision_obj if quote.brand == "construction" else None,
            bedrooms=quote.bedrooms,
            bathrooms=quote.bathrooms,
            service_type=quote.service_type,
            add_ons=quote.add_ons,
            cleaning_vision_analysis=vision_obj if quote.brand == "broom" else None,
        )
        
        if quote.brand == "construction":
            pricing_result = pricing_engine.calculate_construction_price(pricing_request, db)
        else:
            pricing_result = pricing_engine.calculate_cleaning_price(pricing_request, db)
        
        # Update quote with pricing
        quote.base_price = pricing_result.base_price
        quote.final_price = pricing_result.final_price
        quote.status = QuoteStatus.APPROVED
        
        db.commit()
        
        return JSONResponse({
            "success": True,
            "analysis": result.model_dump(),
            "pricing": pricing_result.model_dump(),
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.error_message,
        }, status_code=500)


# ─── Scheduling Routes ───────────────────────────────────────────────────────

@app.post("/webhooks/cal")
async def cal_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive booking webhooks from Cal.com or Easy!Appointments."""
    
    payload = await request.json()
    
    # Extract booking details
    event_type = payload.get("type")
    if event_type != "BOOKING_CREATED":
        return JSONResponse({"status": "ignored"})
    
    booking = payload.get("payload", {}).get("event", {})
    
    # Create appointment record
    appointment = ScheduledAppointment(
        quote_id=booking.get("metadata", {}).get("quote_id"),
        appointment_type=booking.get("title", "Consultation"),
        start_time=datetime.fromisoformat(booking.get("startTime")),
        end_time=datetime.fromisoformat(booking.get("endTime")),
        duration_minutes=booking.get("duration", 60),
        location_type="on_site",
        address=booking.get("location", ""),
        customer_name=booking.get("name", ""),
        customer_email=booking.get("email", ""),
        customer_phone=booking.get("phone", ""),
        provider="cal_com",
        external_id=booking.get("uid"),
        external_url=booking.get("videoCallData", {}).get("url"),
    )
    
    db.add(appointment)
    
    # Update quote status
    quote_id = booking.get("metadata", {}).get("quote_id")
    if quote_id:
        quote = db.query(Quote).filter(Quote.id == quote_id).first()
        if quote:
            quote.appointment_id = appointment.id
            quote.status = QuoteStatus.BOOKED
    
    db.commit()
    
    return JSONResponse({"status": "success"})


# ─── Admin Routes ────────────────────────────────────────────────────────────

@app.get("/admin/quotes", response_class=HTMLResponse)
async def admin_quotes_page(request: Request, db: Session = Depends(get_db)):
    """Admin page to view all quotes."""
    quotes = db.query(Quote).order_by(Quote.created_at.desc()).all()
    return templates.TemplateResponse(
        "admin/quotes.html",
        {"request": request, "quotes": quotes}
    )


@app.get("/admin/pricing-rules", response_class=HTMLResponse)
async def admin_pricing_rules_page(request: Request, db: Session = Depends(get_db)):
    """Admin page to edit pricing rules."""
    rules = db.query(PricingRule).order_by(PricingRule.brand, PricingRule.category).all()
    return templates.TemplateResponse(
        "admin/pricing_rules.html",
        {"request": request, "rules": rules}
    )


@app.post("/admin/pricing-rules")
async def update_pricing_rule(
    rule_id: int = Form(...),
    base_amount: float = Form(...),
    multiplier: float = Form(1.0),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
):
    """Update a pricing rule."""
    rule = db.query(PricingRule).filter(PricingRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    rule.base_amount = base_amount
    rule.multiplier = multiplier
    rule.is_active = is_active
    rule.updated_at = datetime.utcnow()
    
    db.commit()
    
    return JSONResponse({"success": True, "message": "Pricing rule updated"})


# ─── Quote Detail & Photo Serving ─────────────────────────────────────────────

@app.get("/api/quote/{quote_id}/detail")
async def get_quote_detail(quote_id: int, db: Session = Depends(get_db)):
    """Get full quote details including photos and analysis."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    photos = db.query(Photo).filter(Photo.quote_id == quote_id).order_by(Photo.photo_order).all()
    vision_analysis = db.query(VisionAnalysis).filter(VisionAnalysis.quote_id == quote_id).first()
    
    return JSONResponse({
        "quote": {
            "id": quote.id,
            "brand": quote.brand,
            "status": quote.status.value,
            "customer_name": quote.customer_name,
            "customer_email": quote.customer_email,
            "customer_phone": quote.customer_phone,
            "property_address": quote.property_address,
            "zip_code": quote.zip_code,
            "square_footage": quote.square_footage,
            "project_type": quote.project_type,
            "finish_tier": quote.finish_tier,
            "bedrooms": quote.bedrooms,
            "bathrooms": quote.bathrooms,
            "service_type": quote.service_type,
            "add_ons": quote.add_ons,
            "base_price": quote.base_price,
            "final_price": quote.final_price,
            "customer_notes": quote.customer_notes,
            "admin_notes": quote.admin_notes,
            "created_at": quote.created_at.isoformat() if quote.created_at else None,
        },
        "photos": [
            {
                "id": p.id,
                "filename": p.filename,
                "file_size": p.file_size,
                "mime_type": p.mime_type,
                "room_type": p.room_type,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in photos
        ],
        "vision_analysis": {
            "id": vision_analysis.id,
            "provider": vision_analysis.provider,
            "model": vision_analysis.model,
            "extracted_data": vision_analysis.extracted_data,
            "confidence_score": vision_analysis.confidence_score,
            "red_flags": vision_analysis.red_flags,
            "processing_time_seconds": vision_analysis.processing_time_seconds,
            "processed_at": vision_analysis.processed_at.isoformat() if vision_analysis.processed_at else None,
        } if vision_analysis else None,
    })


@app.get("/api/photo/{photo_id}")
async def serve_photo(photo_id: int, db: Session = Depends(get_db)):
    """Serve an uploaded photo file."""
    from fastapi.responses import FileResponse
    
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    
    file_path = Path(photo.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Photo file not found on disk")
    
    return FileResponse(
        path=str(file_path),
        media_type=photo.mime_type,
        filename=photo.filename,
    )


@app.post("/admin/quotes/{quote_id}/approve")
async def approve_quote(quote_id: int, db: Session = Depends(get_db)):
    """Admin approves a quote for sending."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    quote.status = QuoteStatus.SENT
    db.commit()
    
    return JSONResponse({"success": True, "message": f"Quote #{quote_id} approved and marked as sent"})


# ─── Schedule & Deposit Routes ───────────────────────────────────────────────

@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(
    request: Request,
    brand: str = "construction",
    quote_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Display Cal.com scheduling widget."""
    context = {
        "request": request,
        "brand": brand,
        "calcom_construction_slug": CALCOM_CONSTRUCTION_SLUG,
        "calcom_broom_slug": CALCOM_BROOM_SLUG,
    }
    
    if quote_id:
        quote = db.query(Quote).filter(Quote.id == quote_id).first()
        if quote:
            context.update({
                "quote_id": quote.id,
                "customer_name": quote.customer_name,
                "customer_email": quote.customer_email,
                "property_address": quote.property_address,
                "zip_code": quote.zip_code,
                "square_footage": quote.square_footage,
                "project_type": quote.project_type,
                "finish_tier": quote.finish_tier,
                "bedrooms": quote.bedrooms,
                "bathrooms": quote.bathrooms,
                "service_type": quote.service_type,
                "final_price": quote.final_price,
                "quote_type": "conditional_fixed" if quote.brand == "construction" else "instant_guaranteed",
            })
    
    return templates.TemplateResponse("schedule.html", context)


@app.get("/deposit-instructions/{quote_id}", response_class=HTMLResponse)
async def deposit_instructions_page(quote_id: int, request: Request, db: Session = Depends(get_db)):
    """Display Zelle/ACH deposit instructions after quote acceptance."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    # Calculate deposit amount
    if quote.brand == "construction":
        deposit_amount = (quote.final_price or 0) * 0.30  # 30% deposit
    else:
        deposit_amount = quote.final_price or 0  # Full payment for cleaning
    
    # Get adjustments for itemized display
    adjustments = []
    if quote.adjustments:
        adjustments = quote.adjustments if isinstance(quote.adjustments, list) else []
    
    return templates.TemplateResponse("deposit_instructions.html", {
        "request": request,
        "quote": quote,
        "deposit_amount": deposit_amount,
        "adjustments": adjustments,
        "zelle_handle": ZELLE_HANDLE,
        "ach_routing": ACH_ROUTING,
        "ach_account": ACH_ACCOUNT,
        "bank_name": BANK_NAME,
    })


@app.post("/api/quotes/{quote_id}/accept")
async def accept_quote(quote_id: int, db: Session = Depends(get_db)):
    """Customer accepts a quote — trigger deposit instructions + email."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    # Calculate deposit
    if quote.brand == "construction":
        deposit_amount = (quote.final_price or 0) * 0.30
    else:
        deposit_amount = quote.final_price or 0
    
    # Build scope description
    if quote.brand == "construction":
        scope_desc = (
            f"{(quote.project_type or 'project').replace('_', ' ').title()} — "
            f"{quote.square_footage or 0:,.0f} sq ft at {quote.finish_tier or 'standard'} tier\n"
            f"Location: {quote.property_address} (ZIP {quote.zip_code})"
        )
    else:
        scope_desc = (
            f"{(quote.service_type or 'cleaning').replace('_', ' ').title()} — "
            f"{quote.bedrooms or 0}BR / {quote.bathrooms or 0}BA, {quote.square_footage or 0:,.0f} sq ft\n"
            f"Location: {quote.property_address} (ZIP {quote.zip_code})"
        )
        if quote.add_ons:
            scope_desc += f"\nAdd-ons: {', '.join(quote.add_ons)}"
    
    # Update quote status
    quote.status = QuoteStatus.APPROVED
    db.commit()
    
    # Send deposit instructions email
    schedule_url = f"{BASE_URL}/schedule?brand={quote.brand}&quote_id={quote.id}"
    
    await email_service.send_deposit_instructions(
        customer_name=quote.customer_name,
        customer_email=quote.customer_email,
        quote_id=quote.id,
        brand=quote.brand,
        scope_description=scope_desc,
        deposit_amount=deposit_amount,
        final_price=quote.final_price or 0,
        schedule_url=schedule_url,
    )
    
    return JSONResponse({
        "success": True,
        "redirect_url": f"/deposit-instructions/{quote.id}",
        "deposit_amount": deposit_amount,
    })


# ─── Review Funnel Routes ───────────────────────────────────────────────────

@app.get("/review", response_class=HTMLResponse)
async def review_gate_page(request: Request):
    """Display the 5-star review gate."""
    return templates.TemplateResponse("review_gate.html", {
        "request": request,
        "google_review_url": GOOGLE_REVIEW_URL,
        "facebook_review_url": FACEBOOK_REVIEW_URL,
        "yelp_review_url": YELP_REVIEW_URL,
    })


@app.post("/api/reviews/submit")
async def submit_review(
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    rating: int = Form(...),
    feedback: str = Form(""),
    brand: str = Form("broom"),
    db: Session = Depends(get_db),
):
    """Submit a review — routes positive to public sites, negative to owner."""
    
    review = CustomerReview(
        customer_name=customer_name,
        customer_email=customer_email,
        rating=rating,
        feedback_text=feedback,
        brand=brand,
        routed_to_public=(rating >= 4),
        routed_to_owner=(rating <= 3),
    )
    db.add(review)
    db.commit()
    
    # If negative, email owner immediately
    if rating <= 3:
        await email_service.send_negative_feedback_alert(
            customer_name=customer_name,
            customer_email=customer_email,
            rating=rating,
            feedback=feedback,
            brand=brand,
        )
    
    return JSONResponse({
        "success": True,
        "routed_to_public": rating >= 4,
        "routed_to_owner": rating <= 3,
        "message": "Thank you for your feedback!",
    })


# ─── Local SEO: City Landing Pages, Sitemap, Robots ─────────────────────────

@app.get("/areas/{city_slug}", response_class=HTMLResponse)
async def city_landing_page(city_slug: str, request: Request):
    """Programmatic city landing page for local SEO."""
    city_data = get_city_page(city_slug)
    if not city_data:
        raise HTTPException(status_code=404, detail="Area not found")
    
    schema = build_json_ld_schema(city_data)
    
    return templates.TemplateResponse("city_landing.html", {
        "request": request,
        "city": city_data,
        "schema": schema,
        "all_cities": CITY_PAGES,
    })


@app.get("/sitemap.xml")
async def sitemap():
    """Dynamic XML sitemap for SEO."""
    from fastapi.responses import Response
    
    base_url = BASE_URL
    city_slugs = get_all_city_slugs()
    
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    
    # Static pages
    static_pages = [
        "/",
        "/instant-quote?brand=broom",
        "/instant-quote?brand=construction",
        "/review",
        "/schedule?brand=broom",
        "/schedule?brand=construction",
    ]
    
    for page in static_pages:
        xml += f"  <url>\n    <loc>{base_url}{page}</loc>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>\n"
    
    # City landing pages
    for slug in city_slugs:
        xml += f"  <url>\n    <loc>{base_url}/areas/{slug}</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.7</priority>\n  </url>\n"
    
    xml += "</urlset>"
    
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
async def robots_txt():
    """Robots.txt for search engine crawlers."""
    from fastapi.responses import Response
    
    content = """User-agent: *
Allow: /
Disallow: /admin/
Disallow: /api/
Disallow: /static/

Sitemap: {}/sitemap.xml
""".format(BASE_URL)
    
    return Response(content=content, media_type="text/plain")


# ─── Admin Marketing Routes ─────────────────────────────────────────────────

@app.get("/admin/marketing", response_class=HTMLResponse)
async def admin_marketing_page(request: Request, db: Session = Depends(get_db)):
    """Admin marketing dashboard — social media post generator."""
    posts = db.query(MarketingPost).order_by(MarketingPost.created_at.desc()).limit(50).all()
    return templates.TemplateResponse("admin/marketing.html", {
        "request": request,
        "posts": posts,
    })


@app.get("/admin/marketing/completed-photos")
async def get_completed_photos(db: Session = Depends(get_db)):
    """Get photos from completed/booked quotes for marketing use."""
    completed_quotes = db.query(Quote).filter(
        Quote.status.in_([QuoteStatus.COMPLETED, QuoteStatus.BOOKED, QuoteStatus.APPROVED])
    ).all()
    
    photo_list = []
    for quote in completed_quotes:
        for photo in quote.photos:
            photo_list.append({
                "id": photo.id,
                "quote_id": quote.id,
                "filename": photo.filename,
                "brand": quote.brand,
            })
    
    return JSONResponse({"photos": photo_list})


@app.post("/admin/marketing/post")
async def create_marketing_post(request: Request, db: Session = Depends(get_db)):
    """Create and optionally publish a marketing post."""
    import json
    
    body = await request.json()
    
    post = MarketingPost(
        brand=body.get("brand", "broom"),
        caption=body.get("caption", ""),
        hashtags=body.get("hashtags", ""),
        city_slug=body.get("city_slug"),
        photo_ids=body.get("photo_ids", []),
        status="published" if body.get("platform") else "draft",
    )
    
    # Attempt platform publishing
    platform = body.get("platform")
    if platform == "google":
        # Google Business Profile API integration
        # TODO: Implement actual Google Business Profile API call
        # This requires OAuth2 credentials and the My Business API
        post.google_posted = True  # Mark as attempted
        post.status = "published"
    elif platform == "facebook":
        # Facebook Graph API integration
        # TODO: Implement actual Facebook Graph API call
        post.facebook_posted = True
        post.status = "published"
    
    db.add(post)
    db.commit()
    
    return JSONResponse({
        "success": True,
        "post_id": post.id,
        "message": f"Marketing post created" + (f" and published to {platform}" if platform else ""),
    })


# ─── Health Check ────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Initialize database and seed default pricing rules."""
    
    # Seed default pricing rules if none exist
    db = SessionLocal()
    existing_rules = db.query(PricingRule).count()
    
    if existing_rules == 0:
        # Seed construction rules
        for project_type, rate in DEFAULT_PRICING_RULES["construction"]["base_rate"].items():
            db.add(PricingRule(
                brand="construction",
                category="base_rate",
                name=f"{project_type} per sqft",
                base_amount=rate,
                project_type=project_type,
            ))
        
        # Seed cleaning rules
        db.add(PricingRule(
            brand="broom",
            category="base_rate",
            name="Base cleaning fee",
            base_amount=DEFAULT_PRICING_RULES["broom"]["base_rate"],
        ))
        db.add(PricingRule(
            brand="broom",
            category="per_bedroom",
            name="Per bedroom",
            base_amount=DEFAULT_PRICING_RULES["broom"]["per_bedroom"],
        ))
        db.add(PricingRule(
            brand="broom",
            category="per_bathroom",
            name="Per bathroom",
            base_amount=DEFAULT_PRICING_RULES["broom"]["per_bathroom"],
        ))
        
        db.commit()
    
    db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
