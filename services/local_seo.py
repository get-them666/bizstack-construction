"""Local SEO data and helpers for Broom Service city landing pages."""
from __future__ import annotations

from typing import Dict, List, Optional


# ─── City Landing Page Data ─────────────────────────────────────────────────

CITY_PAGES: Dict[str, dict] = {
    "chesapeake-va": {
        "city": "Chesapeake",
        "state": "VA",
        "slug": "chesapeake-va",
        "title": "STR Turnover Cleaning in Chesapeake, VA | Broom Service",
        "description": "Professional short-term rental turnover cleaning in Chesapeake, VA. Guaranteed flat-rate quotes with photo upload. Serving Greenbrier, Deep Creek, Indian River, and all Chesapeake neighborhoods.",
        "keywords": ["Chesapeake cleaning", "STR turnover Chesapeake", "Airbnb cleaning Chesapeake", "vacation rental cleaning Chesapeake VA", "short-term rental cleaning"],
        "hero_headline": "Chesapeake's Trusted STR Turnover Cleaning",
        "hero_subheadline": "Flat-rate quotes in 60 seconds. Photo-powered cleaning for Airbnb & VRBO hosts across Chesapeake — from Greenbrier to Deep Creek.",
        "zip_codes": ["23320", "23321", "23322", "23323", "23324", "23325", "23326", "23327", "23328"],
        "zone": "core",
        "nearby_areas": ["virginia-beach-va", "norfolk-va", "portsmouth-va"],
        "neighborhoods": ["Greenbrier", "Deep Creek", "Indian River", "Western Branch", "South Norfolk", "Hickory", "Great Bridge"],
    },
    "virginia-beach-va": {
        "city": "Virginia Beach",
        "state": "VA",
        "slug": "virginia-beach-va",
        "title": "Airbnb & VRBO Cleaning in Virginia Beach, VA | Broom Service",
        "description": "Top-rated short-term rental cleaning in Virginia Beach. Fast turnover, linen restock, and staging for Oceanfront, Sandbridge, and Town Center hosts.",
        "keywords": ["Virginia Beach cleaning", "Airbnb cleaning Virginia Beach", "VRBO cleaning VB", "oceanfront rental cleaning", "Sandbridge cleaning"],
        "hero_headline": "Virginia Beach STR Cleaning — Oceanfront to Town Center",
        "hero_subheadline": "Same-day turnover for VB hosts. Guaranteed flat rates, no surprise fees. We clean while your guests check out.",
        "zip_codes": ["23451", "23452", "23453", "23454", "23455", "23456", "23457", "23458", "23459", "23460", "23461", "23462", "23464"],
        "zone": "core",
        "nearby_areas": ["chesapeake-va", "norfolk-va"],
        "neighborhoods": ["Oceanfront", "Sandbridge", "Town Center", "Hilltop", "Lynnhaven", "Great Neck", "Kempsville"],
    },
    "norfolk-va": {
        "city": "Norfolk",
        "state": "VA",
        "slug": "norfolk-va",
        "title": "Short-Term Rental Cleaning in Norfolk, VA | Broom Service",
        "description": "Professional STR turnover cleaning for Norfolk hosts. Serving Ghent, Downtown, Hague District, and all Norfolk neighborhoods.",
        "keywords": ["Norfolk cleaning", "STR cleaning Norfolk VA", "Ghent Airbnb cleaning", "Downtown Norfolk cleaning"],
        "hero_headline": "Norfolk STR Cleaning — Ghent to Downtown",
        "hero_subheadline": "Reliable turnover cleaning for Norfolk's growing STR market. Flat-rate quotes with photo upload.",
        "zip_codes": ["23501", "23502", "23503", "23504", "23505", "23507", "23508", "23509", "23510", "23511"],
        "zone": "core",
        "nearby_areas": ["virginia-beach-va", "chesapeake-va", "portsmouth-va"],
        "neighborhoods": ["Ghent", "Downtown", "Hague District", "Freemason", "Larchmont", "Wards Corner"],
    },
    "williamsburg-va": {
        "city": "Williamsburg",
        "state": "VA",
        "slug": "williamsburg-va",
        "title": "Historic Area STR Cleaning in Williamsburg, VA | Broom Service",
        "description": "Short-term rental turnover cleaning in Williamsburg and Yorktown. Serving Colonial Williamsburg area, Jamestown, and York County vacation rental hosts.",
        "keywords": ["Williamsburg cleaning", "STR cleaning Williamsburg VA", "historic area cleaning", "Yorktown rental cleaning", "Colonial Williamsburg Airbnb"],
        "hero_headline": "Williamsburg & Yorktown STR Cleaning",
        "hero_subheadline": "Trusted by Historic Area hosts. Professional turnover cleaning for Colonial Williamsburg, Jamestown, and York County.",
        "zip_codes": ["23185", "23188", "23690", "23691", "23692", "23693"],
        "zone": "core",
        "nearby_areas": ["hampton-newport-news-va", "norfolk-va"],
        "neighborhoods": ["Colonial Williamsburg", "Jamestown", "Yorktown", "Groton", "Lakeside", "College Woods"],
    },
    "hampton-newport-news-va": {
        "city": "Hampton & Newport News",
        "state": "VA",
        "slug": "hampton-newport-news-va",
        "title": "STR Cleaning in Hampton & Newport News, VA | Broom Service",
        "description": "Short-term rental turnover cleaning for Hampton Roads hosts. Serving Hampton, Newport News, and the Peninsula area.",
        "keywords": ["Hampton cleaning", "Newport News cleaning", "Peninsula STR cleaning", "Hampton Roads rental cleaning"],
        "hero_headline": "Peninsula STR Cleaning — Hampton to Newport News",
        "hero_subheadline": "Professional turnover cleaning for Hampton Roads Peninsula hosts. Guaranteed flat rates, reliable scheduling.",
        "zip_codes": ["23661", "23662", "23663", "23664", "23665", "23666", "23601", "23602", "23603", "23604", "23605", "23606", "23607", "23608"],
        "zone": "core",
        "nearby_areas": ["williamsburg-va", "norfolk-va"],
        "neighborhoods": ["Hampton", "Newport News", "Oyster Point", "City Center", "Keepler", "Denbigh"],
    },
    "outer-banks-currituck-nc": {
        "city": "Outer Banks & Currituck",
        "state": "NC",
        "slug": "outer-banks-currituck-nc",
        "title": "Outer Banks & Currituck NC Vacation Rental Cleaning | Broom Service",
        "description": "Vacation rental turnover cleaning for Outer Banks and Currituck County, NC. Serving Corolla, Duck, Southern Shores, and Elizabeth City hosts.",
        "keywords": ["Outer Banks cleaning", "OBX vacation rental cleaning", "Corolla cleaning", "Currituck cleaning", "Elizabeth City cleaning", "Duck NC cleaning"],
        "hero_headline": "Outer Banks & Currituck Vacation Rental Cleaning",
        "hero_subheadline": "Trusted by OBX hosts from Corolla to Duck. Extended zone service with reliable scheduling for peak and off-season turnover.",
        "zip_codes": ["27916", "27921", "27923", "27924", "27927", "27939", "27947", "27949", "27959", "27968", "27976", "27909"],
        "zone": "extended",
        "nearby_areas": ["chesapeake-va", "virginia-beach-va"],
        "neighborhoods": ["Corolla", "Duck", "Southern Shores", "Kitty Hawk", "Kill Devil Hills", "Elizabeth City", "Currituck"],
    },
}


def get_city_page(city_slug: str) -> Optional[dict]:
    """Get city landing page data by slug."""
    return CITY_PAGES.get(city_slug)


def get_all_city_slugs() -> List[str]:
    """Get all city slugs for sitemap."""
    return list(CITY_PAGES.keys())


def build_json_ld_schema(city_data: Optional[dict] = None) -> dict:
    """Build JSON-LD structured data for Broom Service."""
    
    base_schema = {
        "@context": "https://schema.org",
        "@type": "HousekeepingService",
        "name": "Broom Service",
        "description": "Professional short-term rental turnover cleaning, deep cleans, move-out cleaning, and STR co-hosting in the Hampton Roads, VA and Outer Banks, NC area.",
        "url": "https://bizstackperks.com",
        "telephone": "+1-757-908-7121",
        "email": "hello@bizstackperks.com",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "701 Dana Dr",
            "addressLocality": "Chesapeake",
            "addressRegion": "VA",
            "postalCode": "23321",
            "addressCountry": "US"
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": 36.7682,
            "longitude": -76.2875
        },
        "areaServed": [
            {"@type": "City", "name": "Chesapeake", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Virginia Beach", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Norfolk", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Portsmouth", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Suffolk", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Newport News", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Hampton", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Williamsburg", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "City", "name": "Yorktown", "containedInPlace": {"@type": "State", "name": "Virginia"}},
            {"@type": "County", "name": "Currituck County", "containedInPlace": {"@type": "State", "name": "North Carolina"}},
            {"@type": "City", "name": "Elizabeth City", "containedInPlace": {"@type": "State", "name": "North Carolina"}},
        ],
        "sameAs": [
            "https://www.facebook.com/buildstackconstruction",
            "https://www.yelp.com/biz/broom-service-chesapeake",
        ],
        "priceRange": "$$",
        "openingHours": "Mo-Sa 07:00-20:00",
        "paymentAccepted": "Zelle, ACH, Bank Transfer",
        "serviceType": [
            "Short-term rental turnover cleaning",
            "Deep cleaning",
            "Move-out cleaning",
            "Linen restock",
            "STR staging",
            "Co-hosting",
        ],
    }
    
    # Add city-specific overrides
    if city_data:
        base_schema["areaServed"] = [
            {"@type": "City", "name": city_data["city"], "containedInPlace": {"@type": "State", "name": city_data["state"]}}
        ]
        base_schema["description"] = city_data["description"]
    
    return base_schema
