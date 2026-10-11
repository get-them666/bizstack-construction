"""Keyless permit enrichment API - resolves permit tracking emails to permit details.

This is an API wrapper around the existing permit_enricher.enrich() function,
exposed as a FastAPI service for internal/external callers.
"""

import json
import os
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Import the existing enrich function
from permit_enricher import enrich, parse_tracking_email

app = FastAPI(title="Permit Enrichment API", version="1.0.0")

HERE = Path(__file__).parent


class EnrichResponse(BaseModel):
    status: str
    source_id: Optional[str] = None
    lead_ids: Optional[list] = None
    matched_rows: Optional[int] = None
    contractor_name: Optional[str] = None
    property_address: Optional[str] = None
    city: Optional[str] = None
    permit_number: Optional[str] = None
    permit_id: Optional[str] = None
    job_valuation: Optional[int] = None
    permit_type: Optional[str] = None
    lead_status: Optional[str] = None
    message: Optional[str] = None


class EnrichRequest(BaseModel):
    email: str


@app.get("/")
async def index():
    return {"service": "permit-enrichment-api", "version": "1.0.0", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/enrich", response_model=EnrichResponse)
async def enrich_endpoint(request: EnrichRequest):
    """Enrich a permit tracking email into permit details."""
    result = enrich(request.email)
    return EnrichResponse(**result)


@app.get("/enrich", response_model=EnrichResponse)
async def enrich_get(email: str = Query(..., description="Permit tracking email (con-permit-<hash>@lead.local)")):
    """Enrich via GET for simple integrations."""
    result = enrich(email)
    return EnrichResponse(**result)


@app.get("/enrich/batch")
async def enrich_batch(emails: str = Query(..., description="Comma-separated tracking emails")):
    """Enrich multiple emails at once."""
    results = []
    for e in emails.split(","):
        result = enrich(e.strip())
        results.append(result)
    return {"results": results}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)