"""Vision AI Analyzer - Extract structured data from photos using Gemini or OpenAI."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional, Union

import httpx

# Import Pydantic schemas
from schemas import (
    ConstructionVisionSchema,
    CleaningVisionSchema,
    VisionAnalysisRequest,
    VisionAnalysisResponse,
)

# AI Provider Configuration
GEMINI_API_KEY = ""  # Set via environment or config
OPENAI_API_KEY = ""  # Set via environment or config

GEMINI_MODEL = "gemini-2.0-flash"
OPENAI_MODEL = "gpt-4o-mini"

# Timeouts
REQUEST_TIMEOUT = 60  # seconds


# ─── Helper Functions ────────────────────────────────────────────────────────

def image_to_base64(image_path: str) -> str:
    """Convert image file to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_image_mime_type(image_path: str) -> str:
    """Get MIME type from image file extension."""
    ext = Path(image_path).suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return mime_types.get(ext, "image/jpeg")


# ─── Google Gemini Analyzer ─────────────────────────────────────────────────

async def analyze_with_gemini(
    request: VisionAnalysisRequest,
) -> VisionAnalysisResponse:
    """Analyze photos using Google Gemini vision model."""
    start_time = time.time()
    
    if not GEMINI_API_KEY:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message="Gemini API key not configured",
        )
    
    # Determine schema based on brand
    if request.brand == "construction":
        schema = ConstructionVisionSchema
        prompt = """Analyze these construction project photos and extract the following information in JSON format:
{
  "layout_type": "galley|l_shape|u_shape|island|open|exterior|other",
  "estimated_cabinet_linear_ft": <number>,
  "estimated_countertop_sqft": <number>,
  "existing_flooring": "<string>",
  "demolition_complexity": "low|medium|high",
  "red_flags": ["<flag1>", "<flag2>"],
  "confidence_score": <0.0-1.0>
}

If a field cannot be determined, use reasonable defaults based on what's visible.
Red flags include: water_damage, load_bearing_wall, cast_iron_plumbing, outdated_panel, 
soffit_removal, subfloor_rot, asbestos_risk, lead_paint_risk, structural_issue, 
electrical_hazard, mold_detected."""
    else:  # broom
        schema = CleaningVisionSchema
        prompt = """Analyze these cleaning service photos and extract the following information in JSON format:
{
  "soil_and_clutter_level": <1-5>,
  "glass_shower_doors": <number>,
  "pet_hair_detected": <true|false>,
  "recommended_deep_clean_upgrade": <true|false>,
  "confidence_score": <0.0-1.0>
}

Soil levels: 1=spotless, 2=light dust, 3=moderate, 4=heavy, 5=extreme/biohazard.
Recommend deep clean upgrade if soil level >= 4 OR pet hair detected OR heavy clutter."""
    
    # Prepare images
    images = []
    for photo_path in request.photo_paths:
        try:
            base64_data = image_to_base64(photo_path)
            mime_type = get_image_mime_type(photo_path)
            images.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64_data
                }
            })
        except Exception as e:
            return VisionAnalysisResponse(
                success=False,
                quote_id=request.quote_id,
                error_message=f"Failed to read image {photo_path}: {str(e)}",
            )
    
    # Build Gemini request
    gemini_request = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ] + images
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
        }
    }
    
    # Call Gemini API
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    params = {"key": GEMINI_API_KEY}
    
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=gemini_request, params=params)
            response.raise_for_status()
            result = response.json()
        
        # Extract text response
        if "candidates" not in result or not result["candidates"]:
            return VisionAnalysisResponse(
                success=False,
                quote_id=request.quote_id,
                error_message="No candidates returned from Gemini",
            )
        
        raw_text = ""
        for part in result["candidates"][0].get("content", {}).get("parts", []):
            if "text" in part:
                raw_text += part["text"]
        
        # Parse JSON from response
        try:
            # Try to extract JSON from markdown code block
            if "```json" in raw_text:
                json_str = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                json_str = raw_text.split("```")[1].split("```")[0].strip()
            else:
                json_str = raw_text
            
            analysis_data = json.loads(json_str)
            
            # Validate against schema
            validated = schema.model_validate(analysis_data)
            
            processing_time = time.time() - start_time
            
            return VisionAnalysisResponse(
                success=True,
                quote_id=request.quote_id,
                analysis=validated.model_dump(),
                raw_response=raw_text,
                confidence_score=validated.confidence_score,
                red_flags=validated.red_flags if hasattr(validated, 'red_flags') else [],
                processing_time_seconds=processing_time,
            )
        
        except (json.JSONDecodeError, Exception) as e:
            return VisionAnalysisResponse(
                success=False,
                quote_id=request.quote_id,
                raw_response=raw_text,
                error_message=f"Failed to parse Gemini response: {str(e)}",
            )
    
    except httpx.HTTPStatusError as e:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message=f"Gemini API error: {e.response.status_code} - {e.response.text}",
        )
    except Exception as e:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message=f"Gemini request failed: {str(e)}",
        )


# ─── OpenAI Analyzer ────────────────────────────────────────────────────────

async def analyze_with_openai(
    request: VisionAnalysisRequest,
) -> VisionAnalysisResponse:
    """Analyze photos using OpenAI vision model."""
    start_time = time.time()
    
    if not OPENAI_API_KEY:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message="OpenAI API key not configured",
        )
    
    # Determine schema and prompt
    if request.brand == "construction":
        schema = ConstructionVisionSchema
        system_prompt = """You are a construction project analyzer. Analyze the provided photos and return a JSON object with these exact fields:
{
  "layout_type": "galley|l_shape|u_shape|island|open|exterior|other",
  "estimated_cabinet_linear_ft": <number>,
  "estimated_countertop_sqft": <number>,
  "existing_flooring": "<string>",
  "demolition_complexity": "low|medium|high",
  "red_flags": ["<flag1>", "<flag2>"],
  "confidence_score": <0.0-1.0>
}

Red flags: water_damage, load_bearing_wall, cast_iron_plumbing, outdated_panel, 
soffit_removal, subfloor_rot, asbestos_risk, lead_paint_risk, structural_issue, 
electrical_hazard, mold_detected. Return empty array if none detected."""
    else:  # broom
        schema = CleaningVisionSchema
        system_prompt = """You are a cleaning service analyzer. Analyze the provided photos and return a JSON object with these exact fields:
{
  "soil_and_clutter_level": <1-5>,
  "glass_shower_doors": <number>,
  "pet_hair_detected": <true|false>,
  "recommended_deep_clean_upgrade": <true|false>,
  "confidence_score": <0.0-1.0>
}

Soil levels: 1=spotless, 2=light dust, 3=moderate, 4=heavy, 5=extreme/biohazard.
Recommend deep clean if soil >= 4 OR pet hair OR heavy clutter."""
    
    # Prepare messages with images
    messages = [
        {"role": "system", "content": system_prompt}
    ]
    
    # Add user message with images
    user_content = [{"type": "text", "text": "Analyze these photos and return the JSON analysis."}]
    
    for photo_path in request.photo_paths:
        try:
            base64_data = image_to_base64(photo_path)
            mime_type = get_image_mime_type(photo_path)
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_data}"
                }
            })
        except Exception as e:
            return VisionAnalysisResponse(
                success=False,
                quote_id=request.quote_id,
                error_message=f"Failed to read image {photo_path}: {str(e)}",
            )
    
    messages.append({"role": "user", "content": user_content})
    
    # Call OpenAI API
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
        
        # Extract text response
        raw_text = result["choices"][0]["message"]["content"]
        
        # Parse JSON from response
        try:
            # Try to extract JSON from markdown code block
            if "```json" in raw_text:
                json_str = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                json_str = raw_text.split("```")[1].split("```")[0].strip()
            else:
                json_str = raw_text
            
            analysis_data = json.loads(json_str)
            
            # Validate against schema
            validated = schema.model_validate(analysis_data)
            
            processing_time = time.time() - start_time
            
            return VisionAnalysisResponse(
                success=True,
                quote_id=request.quote_id,
                analysis=validated.model_dump(),
                raw_response=raw_text,
                confidence_score=validated.confidence_score,
                red_flags=validated.red_flags if hasattr(validated, 'red_flags') else [],
                processing_time_seconds=processing_time,
            )
        
        except (json.JSONDecodeError, Exception) as e:
            return VisionAnalysisResponse(
                success=False,
                quote_id=request.quote_id,
                raw_response=raw_text,
                error_message=f"Failed to parse OpenAI response: {str(e)}",
            )
    
    except httpx.HTTPStatusError as e:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message=f"OpenAI API error: {e.response.status_code} - {e.response.text}",
        )
    except Exception as e:
        return VisionAnalysisResponse(
            success=False,
            quote_id=request.quote_id,
            error_message=f"OpenAI request failed: {str(e)}",
        )


# ─── Unified Analyzer Interface ──────────────────────────────────────────────

async def analyze_photos(
    request: VisionAnalysisRequest,
    provider: str = "gemini",  # "gemini" or "openai"
) -> VisionAnalysisResponse:
    """
    Main entry point for vision analysis.
    
    Routes to appropriate provider based on configuration.
    Falls back to OpenAI if Gemini fails, and vice versa.
    """
    if provider == "gemini":
        result = await analyze_with_gemini(request)
        if not result.success:
            # Fallback to OpenAI
            result = await analyze_with_openai(request)
        return result
    else:  # openai
        result = await analyze_with_openai(request)
        if not result.success:
            # Fallback to Gemini
            result = await analyze_with_gemini(request)
        return result


# ─── Example Usage ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Example: Analyze construction photos
    import asyncio
    
    async def main():
        request = VisionAnalysisRequest(
            quote_id=1,
            brand="construction",
            photo_paths=["/path/to/photo1.jpg", "/path/to/photo2.jpg"],
            project_type="kitchen",
            square_footage=200.0,
        )
        
        result = await analyze_photos(request, provider="gemini")
        print(json.dumps(result.model_dump(), indent=2))
    
    asyncio.run(main())
